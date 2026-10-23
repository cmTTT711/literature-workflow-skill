from __future__ import annotations
import json, os, re, hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

def data_home() -> Path:
    return Path(os.environ.get("SCANPDF_HOME") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ScanPDF").expanduser().resolve()

def atomic_json(path: Path, value) -> None:
    import time
    from uuid import uuid4
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(20):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 19:raise
                time.sleep(.05)
    finally:
        tmp.unlink(missing_ok=True)

@dataclass
class Config:
    school: str = ""
    school_en: str = ""
    email: str = ""
    access: str = "campus"
    institution_url: str = ""
    output_dir: str = ""
    browser_executable: str = ""
    network_proxy: str = ""
    headless: bool = False
    challenge_wait: int = 45
    login_timeout: int = 300
    max_file_mb: int = 100
    backend_overrides: dict = field(default_factory=dict)
    onboarding: dict = field(default_factory=dict)
    home: Path = field(default_factory=data_home, repr=False)

    def __post_init__(self):
        self.home = Path(self.home).expanduser().resolve()
        if not self.output_dir:
            self.output_dir = str(Path.home() / "Downloads" / "ScanPDF")

    @classmethod
    def load(cls, home: Path | None = None):
        h = Path(home or data_home()).resolve()
        p = h / "config.json"
        if not p.exists(): return cls(home=h)
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls(**{k:v for k,v in raw.items() if k in cls.__dataclass_fields__ and k != "home"}, home=h)

    def save(self):
        self.validate()
        raw = asdict(self);raw.pop("home")
        atomic_json(self.home / "config.json", raw)

    def validate(self):
        if not self.school.strip(): raise ValueError("请配置学校全名：scanpdf setup")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email) or self.email.endswith((".invalid", "@example.com")):
            raise ValueError("请提供可联系的真实邮箱")
        if self.access not in {"campus", "carsi", "institution"}: raise ValueError("无效的机构访问方式")
        from urllib.parse import urlsplit
        if self.access == "institution" and (urlsplit(self.institution_url).scheme != "https" or not urlsplit(self.institution_url).hostname):
            raise ValueError("请提供学校图书馆官方 HTTPS 认证入口")
        if not 0 <= self.challenge_wait <= 600 or not 1 <= self.login_timeout <= 1800:
            raise ValueError("等待时间超出有效范围")

    @property
    def school_id(self):
        return hashlib.sha256(self.school.strip().casefold().encode()).hexdigest()[:12]

    def profile(self, publisher: str, backend: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_-]+", publisher+backend): raise ValueError("非法会话名称")
        engine_dir="camoufox-152.0.4-beta.30" if backend=="camoufox" else backend
        return self.home / "profiles" / self.school_id / engine_dir / publisher

    def browser_config(self, backend: str):
        return {"browser_backend":backend, "browser_executable":self.browser_executable,
                "browser_auto_upgrade":backend!="cloakbrowser", "network_proxy":self.network_proxy}
