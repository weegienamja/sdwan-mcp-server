"""Interactive, session-only SD-WAN diagnostics on an operator's laptop."""

import asyncio
import re
import shutil
from pathlib import Path

import click

from cisco_vmanage_mcp.services.local_model import LocalModel, LocalModelError, run_local_model
from cisco_vmanage_mcp.services.workstation import ManagerReader, ManagerReadError, manager_origin
from cisco_vmanage_mcp.webapp import EvidenceAssistant


def _choose_model(downloads: Path) -> Path:
    candidates = []
    for file in sorted(downloads.glob("*.gguf")):
        name = file.name.lower()
        if not file.is_file() or "qwen3.5-4b" not in name or name.startswith(("mtp-", "mmproj-")):
            continue
        shard = re.search(r"-(\d{5})-of-\d{5}\.gguf$", name)
        if shard is None or shard[1] == "00001":
            candidates.append(file)
    if len(candidates) == 1:
        click.echo(f"Using {candidates[0]}")
        return candidates[0]
    if candidates:
        for index, candidate in enumerate(candidates, 1):
            click.echo(f"{index}. {candidate.name}")
        index = click.prompt("Choose your GGUF", type=click.IntRange(1, len(candidates)))
        return candidates[index - 1]
    value = click.prompt("Path to your downloaded Qwen3.5-4B .gguf").strip().strip('"')
    path = Path(value).expanduser()
    if not path.is_file():
        raise click.ClickException("The selected GGUF file could not be found.")
    return path


def _display(value: str) -> None:
    # Model output and device names are untrusted text on a terminal.
    click.echo("".join(c for c in value if c in "\n\t" or c.isprintable()))


async def _answer(assistant: EvidenceAssistant, model: LocalModel, reader: ManagerReader, question: str) -> None:
    result = await assistant.ask(question)
    if reader.sign_in_required:
        raise ManagerReadError("Your Manager session expired. Restart and enter a valid token.")
    sources = result["evidence"]
    for source in sources:
        _display(f"  {source['label']} [{source['state']}]")
    if not any(source["state"] == "ok" for source in sources):
        click.echo("Manager returned no usable evidence for this question.")
        return
    if any(source["state"] != "ok" for source in sources):
        result = {**result, "answer": "Partial evidence. Failed sources are unavailable."}
    _display(result["answer"])
    _display(await model.explain(question, result))


async def _chat(reader: ManagerReader, model: LocalModel) -> None:
    inventory = await reader.get("/dataservice/device")
    count = len(inventory["data"])
    click.echo(f"Authenticated. {count} device{'s' if count != 1 else ''} visible to your Cisco account.")
    click.echo("Ask about fabric health, critical alarms, a site ID or a device system IP. Type /quit to end.")
    assistant = EvidenceAssistant(reader)
    while True:
        question = click.prompt("Ask", prompt_suffix=" > ").strip()
        if question.lower() in {"/quit", "/exit"}:
            return
        if len(question) > 2000:
            click.echo("Keep the question under 2,000 characters.")
            continue
        try:
            await _answer(assistant, model, reader, question)
        except (ManagerReadError, LocalModelError) as exc:
            if reader.sign_in_required:
                raise
            _display(str(exc))


async def _run(
    model_file: Path, executable: Path, manager: str | None, ca_file: Path | None, check_model: bool,
) -> None:
    click.echo("Loading the GGUF on this laptop…")
    async with run_local_model(executable, model_file) as model:
        if check_model:
            answer = await model.explain("How many devices in this synthetic example?", {
                "data": {"device_count": 3}, "evidence": [{"label": "Synthetic inventory"}],
            })
            _display(answer)
            return
        origin = manager_origin(manager or click.prompt("Cisco SD-WAN Manager HTTPS hostname"))
        click.echo(f"Sign in to {origin} with company SSO and MFA.")
        if click.confirm("Open Manager in your browser?", default=True):
            click.launch(str(origin))
        click.echo("In My Profile, copy your personal API Token. It is kept in memory for this run.")
        token = click.prompt("Manager API token", hide_input=True).strip()
        reader = ManagerReader(str(origin), token, ca_file)
        try:
            await _chat(reader, model)
        finally:
            await reader.close()


@click.command()
@click.option("--model", "model_file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Local GGUF. Defaults to Qwen3.5-4B in Downloads.")
@click.option("--llama-server", "server", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Approved llama-server executable. Defaults to the copy on PATH.")
@click.option("--manager", help="Cisco Manager HTTPS origin.")
@click.option("--ca-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Additional PEM trust bundle for Manager.")
@click.option("--check-model", is_flag=True, help="Test local inference with synthetic data and exit.")
def main(model_file, server, manager, ca_file, check_model):
    """Demarc. SD-WAN diagnostics by Jamie Blair."""
    click.echo("Demarc\nSD-WAN diagnostics by Jamie Blair\n")
    executable = server or shutil.which("llama-server")
    if not executable:
        raise click.ClickException("Use the workstation kit or supply --llama-server PATH.")
    try:
        model_file = model_file or _choose_model(Path.home() / "Downloads")
        asyncio.run(_run(model_file.resolve(strict=True), Path(executable).resolve(strict=True),
                         manager, ca_file, check_model))
    except (LocalModelError, ManagerReadError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
