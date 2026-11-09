from flask import Flask

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-hardcoded"


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(debug=True)
