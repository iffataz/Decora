"""Flask app: search form → /process_url runs the pipeline and renders results."""
import os
import re
import logging
from io import BytesIO
from flask import Flask, render_template, redirect, url_for, request, flash
from dotenv import load_dotenv
from PIL import Image
import decora
from sender import gen_everything

load_dotenv()

app = Flask(__name__, template_folder='web', static_folder='web/stat')
app.secret_key = os.environ['SECRET_KEY']
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024

MAX_PHOTO_BYTES = 8 * 1024 * 1024


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

    if not art or not re.match(r'^[a-zA-Z0-9 ]{1,50}$', art):
        flash("Please enter a valid furniture type (letters and spaces only, max 50 characters)")
        return redirect(url_for('home'))

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
            if room_img.mode != "RGB":
                room_img = room_img.convert("RGB")
        except Exception:
            flash("That file doesn't look like a valid image.")
            return redirect(url_for('home'))
    else:
        if not url.startswith(('http://', 'https://')):
            flash("Please enter a valid image URL starting with http:// or https://")
            return redirect(url_for('home'))
        try:
            room_img = decora._fetch_image(url)
        except Exception:
            flash("Could not load an image from that URL.")
            return redirect(url_for('home'))

    try:
        dump = gen_everything(room_img, art)
    except Exception as e:
        logging.exception("Error in gen_everything")
        return render_template("error.html", message=f"Something went wrong: {e}")
    if not dump:
        return render_template("error.html", message="No matching products found. Try a different image or furniture type.")
    return render_template("test_results.html", my_list=dump)


@app.route("/result")
def result():
    return redirect(url_for('home'))


@app.errorhandler(413)
def too_large(e):
    flash("Upload is too large (max 8 MB).")
    return redirect(url_for('home'))


if __name__ == "__main__":
    app.run(debug=True)
