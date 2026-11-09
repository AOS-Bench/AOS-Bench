import subprocess


def run_user_command(user_input: str) -> str:
    completed = subprocess.run(
        f"echo {user_input}",
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout
