from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS_DIR = ROOT / "tokens"

def read_secret_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Secret file not found: {path}")
    return path.read_text(encoding="utf-8").strip()

def get_doctor_token() -> str:
    return read_secret_file(TOKENS_DIR / "doctor.tok")

def get_admin_token() -> str:
    return read_secret_file(TOKENS_DIR / "admin.tok")

def get_admin_access_pass() -> str:
    return read_secret_file(TOKENS_DIR / "admin_access.pass")