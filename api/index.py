"""Vercel entry point: the cloud control room (dashboard + worker sync API).

Every request is routed here (see vercel.json). Vercel's Python runtime looks for a top-level
``handler`` class (a BaseHTTPRequestHandler); ours comes from video_workflow.cloud.app.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from video_workflow.cloud.app import make_cloud_handler  # noqa: E402

_CloudHandler = make_cloud_handler()


class handler(_CloudHandler):  # noqa: N801 - the name Vercel's runtime looks for
    pass
