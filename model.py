from flask import Flask, render_template, redirect, url_for, request, session
from sender import gen_everything

app = Flask(__name__, template_folder='web', static_folder='web/stat')
app.secret_key = 'super secret key'

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
    if request.method == 'POST':
        session['url'] = request.form['url']
        session['art'] = request.form['art']
        print(session['url'],session['art'])


    return redirect(url_for('result'))  # Example redirect

@app.route("/result")
def result():
    dump = gen_everything(session['url'],session['art'])
    print(dump)
    return render_template("test_results.html", my_list =dump)

if __name__ == "__main__":
    app.run(debug=True)