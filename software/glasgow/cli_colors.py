import os

class CliColor:
    GREEN = '\033[92m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BG_RED = '\033[101m'
    ENDC = '\033[0m'

    Disabled = False

def _cli(s: str, c: str) -> str:
    if os.name == 'nt' or CliColor.Disabled:
        return s
    return f"{c}{s}{CliColor.ENDC}"

def cli_green(s: str) -> str:
    return _cli(s, CliColor.GREEN)

def cli_yellow(s: str) -> str:
    return _cli(s, CliColor.YELLOW)

def cli_red(s: str) -> str:
    return _cli(s, CliColor.RED)

def cli_blue(s: str) -> str:
    return _cli(s, CliColor.BLUE)
