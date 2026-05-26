#we will run this app.py to just host the aice_simple.png file on localhost:5000
from flask import Flask, send_file
app = Flask(__name__)
@app.route('/')
def serve_image():
    return send_file('aice_final.jpg', mimetype='image/jpeg')   
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
    