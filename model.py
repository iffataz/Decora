import os
import re
import logging
from flask import Flask, render_template, redirect, url_for, request, session, flash
from dotenv import load_dotenv
from sender import gen_everything

load_dotenv()

app = Flask(__name__, template_folder='web', static_folder='web/stat')
app.secret_key = os.environ['SECRET_KEY']


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/aboutus")
def aboutus():
    return render_template("about.html")


@app.route("/search")
def search():
    return render_template("projects.html")


@app.route("/process_url", methods=['POST'])
def process_url():
    url = request.form.get('url', '').strip()
    art = request.form.get('art', '').strip()

    if not url or not url.startswith(('http://', 'https://')):
        flash("Please enter a valid image URL starting with http:// or https://")
        return redirect(url_for('search'))

    if not art or not re.match(r'^[a-zA-Z0-9 ]{1,50}$', art):
        flash("Please enter a valid furniture type (letters and spaces only, max 50 characters)")
        return redirect(url_for('search'))

    session['url'] = url
    session['art'] = art
    return redirect(url_for('result'))


@app.route("/result")
def result():
    url = session.get('url')
    art = session.get('art')
    if not url or not art:
        flash("Please submit the search form first.")
        return redirect(url_for('search'))
    try:
        dump = gen_everything(url, art)
    except Exception as e:
        logging.exception("Error in gen_everything")
        return render_template("error.html", message=f"Something went wrong: {e}")
    if not dump:
        return render_template("error.html", message="No matching products found. Try a different image or furniture type.")
    return render_template("test_results.html", my_list=dump)


if __name__ == "__main__":
    app.run(debug=True)
