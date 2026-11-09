# Three-Repo Review

## repo_alpha
- finding: Hardcoded SECRET_KEY in Flask app configuration poses a security risk
- file: ./repos/repo_alpha/app.py
- fix: Load SECRET_KEY from environment variable with a fallback warning, e.g., `app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-only-fallback"`

## repo_beta
- finding: Shell injection vulnerability due to unsanitized user input being interpolated into shell command with `shell=True`
- file: repos/repo_beta/service.py
- fix: Change `subprocess.run(f"echo {user_input}", shell=True, ...)` to `subprocess.run(["echo", user_input], shell=False, ...)` to prevent command injection attacks

## repo_gamma
- finding: The yaml.load() function uses yaml.Loader which is unsafe and can execute arbitrary code when loading untrusted YAML files
- file: repos/repo_gamma/config_loader.py
- fix: Replace `yaml.load(f, Loader=yaml.Loader)` with `yaml.safe_load(f)` to prevent potential security vulnerabilities from arbitrary code execution
