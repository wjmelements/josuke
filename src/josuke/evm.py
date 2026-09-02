from subprocess import run


def execute(initcode_hex: str) -> str:
    """Run `evm -x`: execute creation bytecode, return the deployed runtime hex."""
    return run(
        ["evm", "-x"],
        input=initcode_hex,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
