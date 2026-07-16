import warnings

# uvicorn[standard] uses websockets's legacy server adapter, which emits a
# DeprecationWarning ("remove second argument of ws_handler") about an
# upstream API change inside websockets itself. The warning is irrelevant to
# our app code and will be resolved by a future uvicorn / websockets release;
# silence it specifically (do not blanket-silence DeprecationWarning).
warnings.filterwarnings(
    "ignore",
    message="remove second argument of ws_handler",
    category=DeprecationWarning,
)

from app.main import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)