"""Entry point for Iris."""
import base64
import json
import logging
import os
import typing

import flask

import util.gcp_utils
from plugin import Plugin
from util import pubsub_utils, gcp_utils, utils, config_utils

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

gcp_utils.set_env()

app = flask.Flask(__name__)


def __init_flaskapp():
    logging.info("Initializing Iris in process %s", os.getpid())
    Plugin.init()


__init_flaskapp()


@app.route("/")
def index():
    logging.info("environ", os.environ)
    return "I don't think you meant to be here...", 200


# TODO Maybe use Cloud Tasks instead of Pubsub to allow delay, which
# is necessary for labeling objects that are initializing, especially CloudSQL
# Yet,
# 1. Though Iris(Py2) used TaskQueue, it did NOT use delayed delivery  though it easily could  have.
# 2. Apparently objects other than CloudSQL get correctly labeled
# 'on-demand' based on the log event.

@app.route("/schedule", methods=["GET"])
def schedule():
    """
    Send out a message per-plugin per-project to label all objects of that type and project.
    """
    is_cron = flask.request.headers.get("X-Appengine-Cron")
    if not is_cron:
        return "Access Denied: No token or Cron header found", 403

    projects = gcp_utils.get_all_projects()
    projects = filter(lambda p: config_utils.is_project_included(p), projects)
    for project_id in projects:
        for plugin_cls in Plugin.subclasses:
            pubsub_utils.publish(
                msg=json.dumps({"project_id": project_id,
                                "plugin": plugin_cls.__name__}),
                topic_id=pubsub_utils.requestfulllabeling_topic()
            )

    return "OK", 200


@app.route("/label_one", methods=["POST"])
def label_one():
    """Pubsub push endpoint for messages from the Log Sink
    """
    # Performance issue: There are multiple messages for each object-creation, for example
    # one for request and one for response. So, we may be labeling each object multiple times,
    # which is a waste of resources.
    #
    # Or maybe the first one, on request, fails, becuase the resource is not initialized, and
    # then the second one succeeds; need to check that.
    data = __extract_pubsub_content()

    method_from_log = data["protoPayload"]["methodName"]
    for plugin_cls in Plugin.subclasses:
        # We can override on_demand using an arg from Flask; this is forr
        # testing
        if plugin_cls.is_on_demand() or \
                "true" == flask.request.args.get("override_on_demand"):
            plugin = plugin_cls()
            for supported_method in plugin.method_names():
                if supported_method.lower() in method_from_log.lower():
                    gcp_object = plugin.get_gcp_object(data)
                    if gcp_object is not None:
                        project_id = data["resource"]["labels"]["project_id"]
                        if config_utils.is_project_included(project_id):
                            logging.info("Calling %s.label_one() in %s", plugin.__class__.__name__, project_id)
                            plugin.label_one(gcp_object, project_id)
                            plugin.do_batch()
                    else:
                        logging.error(
                            "Cannot find gcp_object to label based on %s",
                            utils.shorten(str(data.get("resource")), 300))

        else:
            assert plugin_cls.__name__ == "Cloudsql", (
                    "For now, there is only one non-on-demand plugin. Change this assertion as needed. "
                    "Found %s" % plugin_cls.__name__
            )

    return "OK", 200


def __extract_pubsub_content() -> typing.Dict:
    __check_pubsub_verification_token()

    envelope = flask.request.get_json()
    if not envelope:
        raise FlaskException("Expect JSON")

    data = json.loads(base64.b64decode(envelope["message"]["data"]))
    return data


@app.route("/do_label", methods=["POST"])
def do_label():
    """Receive a push message from PubSub, sent from schedule() above,
    with instructions to label all objects of a given plugin and project_id.
    """
    data = __extract_pubsub_content()

    plugin_class_localname = data["plugin"]
    plugin = Plugin.create_plugin(plugin_class_localname)
    project_id = data["project_id"]
    logging.info(
        "do_label() for %s in %s",
        plugin.__class__.__name__,
        project_id)
    plugin.do_label(project_id)
    return "OK", 200


def __check_pubsub_verification_token():
    """ Token verifying that only PubSub accesses PubSub push endpoints"""
    known_token = util.gcp_utils.pubsub_token()
    if not known_token:
        raise FlaskException(f"Should define token in env {os.environ}", 400)

    token_from_args = flask.request.args.get("token", "")
    if known_token != token_from_args:
        raise FlaskException(
            f'Access denied: Invalid token "{known_token}"', 403)


class FlaskException(Exception):
    status_code = 400

    def __init__(self, message, status_code=None, payload=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        rv = dict(self.payload or ())
        rv["message"] = self.message
        return rv


@app.errorhandler(FlaskException)
def handle_invalid_usage(error):
    logging.exception(error)
    response = flask.jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


if __name__ in ["__main__"]:
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    port = os.environ.get("PORT", 8000)
    logging.info("Running __main__ for main.py, port %s", port)
    app.run(host="127.0.0.1", port=port, debug=True)
