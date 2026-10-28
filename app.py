"""Flask app: the search form, the pipeline, and result pages.

POST /process_url runs a search and redirects to GET /r/<token>, so the result
has its own address: refreshing it re-reads the store rather than re-submitting
the form, and the link can be kept or shared. See resultstore.py.
"""
import logging
import os
import re
from io import BytesIO

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, url_for
from google.genai import errors as genai_errors
from PIL import Image

import decora
import ratelimit
import resultstore
from sender import gen_everything

load_dotenv()

app = Flask(__name__, template_folder='web', static_folder='web/stat')

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    # Raised at import so the cause is visible in the logs; reading os.environ
    # directly would surface only a bare KeyError from a serverless cold start.
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. Generate one with: "
        'python -c "import secrets; print(secrets.token_hex(32))"'
    )
app.secret_key = SECRET_KEY

MAX_PHOTO_BYTES = 8 * 1024 * 1024
app.config['MAX_CONTENT_LENGTH'] = MAX_PHOTO_BYTES

FURNITURE_TYPE_RE = re.compile(r'^[a-zA-Z0-9 ]{1,50}$')
# secrets.token_urlsafe output: a bad token should 404, not reach the store.
RESULT_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')

logger = logging.getLogger(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/process_url", methods=['POST'])
def process_url():
    photo = request.files.get('photo')
    has_photo = bool(photo and photo.filename)
    url = request.form.get('url', '').strip()
    art = request.form.get('art', '').strip()

    if has_photo and url:
        flash("Please provide either a photo or a URL, not both.")
        return redirect(url_for('home'))

    if not has_photo and not url:
        flash("Please upload a room photo or paste an image URL.")
        return redirect(url_for('home'))

    if not FURNITURE_TYPE_RE.match(art):
        flash("Please enter a valid furniture type (letters, numbers and spaces only, max 50 characters)")
        return redirect(url_for('home'))

    # Metered here: after the cheap checks, so a form typo doesn't cost the
    # visitor a slot, but before the URL fetch and the two Gemini calls.
    decision = ratelimit.check(ratelimit.client_ip(request))
    if not decision.allowed:
        wait = ratelimit.describe_wait(decision.retry_after)
        if decision.scope == "global":
            message = (
                "Decora has hit its daily search limit, which keeps the AI bill "
                f"predictable. Please try again in {wait}."
            )
        else:
            message = f"You've run a few searches already. Please try again in {wait}."
        return (
            render_template("error.html", heading="Too many searches", message=message),
            429,
            {"Retry-After": str(decision.retry_after)},
        )

    if has_photo:
        if not photo.mimetype or not photo.mimetype.startswith('image/'):
            flash("Please upload a valid image file.")
            return redirect(url_for('home'))
        if request.content_length and request.content_length > MAX_PHOTO_BYTES:
            flash("Image is too large (max 8 MB).")
            return redirect(url_for('home'))
        data = photo.read()
        if len(data) > MAX_PHOTO_BYTES:
            flash("Image is too large (max 8 MB).")
            return redirect(url_for('home'))
        try:
            room_img = Image.open(BytesIO(data))
            room_img.load()
            if room_img.mode != "RGB":
                room_img = room_img.convert("RGB")
        except Exception:
            flash("That file doesn't look like a valid image.")
            return redirect(url_for('home'))
    else:
        # Cheap, leak-free check so an obvious typo gets a useful message;
        # fetch_user_image still does the real scheme and address validation.
        if not url.startswith(('http://', 'https://')):
            flash("Please enter a valid image URL starting with http:// or https://")
            return redirect(url_for('home'))
        try:
            room_img = decora.fetch_user_image(url)
        except Exception:
            # Deliberately uniform: a message that distinguished "blocked
            # address" from "fetch failed" would turn this into a port scanner.
            logger.info("Rejected or unreachable room image URL", exc_info=True)
            flash("Could not load an image from that URL.")
            return redirect(url_for('home'))

    store = resultstore.default_store()

    # An identical earlier search is already paid for, so reuse its page rather
    # than spending two more Gemini calls on the same answer.
    key = resultstore.search_key(room_img, art)
    existing = store.token_for(key)
    if existing:
        logger.info("reusing stored search %s", existing)
        return _see_other(existing)

    try:
        dump = gen_everything(room_img, art)
    except genai_errors.APIError:
        logger.exception("Gemini API error in gen_everything")
        return _see_other(store.put_error(
            None, "Our AI stylist is busy right now. Please try again in a minute."))
    except Exception:
        # Logged in full, shown generically: exception text here can carry
        # upstream URLs and internal detail.
        logger.exception("Error in gen_everything")
        return _see_other(store.put_error(
            None, "Something went wrong on our end. Please try again."))

    if not dump:
        return _see_other(store.put_error(
            "No matches yet",
            "No matching products found. Try a different image or furniture type."))

    return _see_other(store.put_results(dump, key=key))


def _see_other(token):
    """303 after a POST: the browser re-requests with GET, so a refresh of the
    result page never re-submits the search."""
    return redirect(url_for('result', token=token), code=303)


@app.route("/r/<token>")
def result(token):
    """A stored search outcome. Cheap, idempotent, and safe to refresh."""
    if not RESULT_TOKEN_RE.match(token):
        abort(404)

    payload = resultstore.default_store().get(token)
    if payload is None:
        # Either the TTL passed, or this instance is not the one that stored it
        # (see resultstore.py on the in-process backend).
        return render_template(
            "error.html",
            heading="That link has expired",
            message="Result links don't last forever. Run the search again to get fresh matches.",
        ), 404

    if "error" in payload:
        error = payload["error"]
        return render_template(
            "error.html",
            heading=error.get("heading"),
            message=error.get("message"),
        )

    return render_template("results.html", my_list=payload.get("items", []))


@app.errorhandler(413)
def too_large(_e):
    flash("Upload is too large (max 8 MB).")
    return redirect(url_for('home'))


if __name__ == "__main__":
    app.run(debug=True)
