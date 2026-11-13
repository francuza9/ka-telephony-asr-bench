"""Command line entry point (stubs)."""

import typer

app = typer.Typer(help="Georgian telephony ASR benchmark.")


@app.command()
def prepare() -> None:
    """Download and build manifests for the datasets."""
    raise NotImplementedError


@app.command()
def transform() -> None:
    """Apply the telephony transform to a manifest."""
    raise NotImplementedError


@app.command()
def run() -> None:
    """Run models over a manifest and store hypotheses."""
    raise NotImplementedError


@app.command()
def score() -> None:
    """Compute WER/CER with bootstrap confidence intervals."""
    raise NotImplementedError


@app.command()
def report() -> None:
    """Build result tables for the paper."""
    raise NotImplementedError


if __name__ == "__main__":
    app()
