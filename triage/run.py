"""Launch the Triage local web server."""
import uvicorn

if __name__ == "__main__":
    # a different port from VTT, so both can run side by side
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8758, reload=False)
