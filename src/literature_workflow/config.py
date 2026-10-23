"""User-local configuration; credentials never enter run profiles or release files."""
from dataclasses import dataclass, asdict, field
from pathlib import Path
import json, os, re

KEY_NAMES = ("OPENALEX_API_KEY", "SCOPUS_API_KEY", "SCOPUS_INSTTOKEN", "WOS_API_KEY",
             "LENS_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "OPENAIRE_TOKEN",
             "PAPER_CONTACT_EMAIL", "UNPAYWALL_EMAIL")
PROVIDER_KEYS = {"openalex": "OPENALEX_API_KEY", "scopus": "SCOPUS_API_KEY",
                 "wos": "WOS_API_KEY", "lens": "LENS_API_KEY",
                 "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY"}
REQUIRED_KEYS = {"scopus", "wos", "lens"}

def atomic_json(path, data):
    from paper_acquisition.library.store import atomic_json as save
    save(Path(path), data)

def default_home():
    # Store-packaged Codex virtualizes LOCALAPPDATA into a very long path.
    # Browser archives/profiles can then exceed Win32 MAX_PATH even before use.
    return Path(os.environ.get("LITERATURE_WORKFLOW_HOME") or Path.home() / ".literature-workflow").resolve()

def read_credentials(path):
    values = {}
    if Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$", line)
            if not match or match[1] not in KEY_NAMES:
                continue
            value = match[2].strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0:
                    raise ValueError("凭据文件有未闭合的引号")
                value = value[1:end]
            else:
                value = value.split(" #", 1)[0].strip()
            values[match[1]] = value
    return {name: os.environ.get(name, values.get(name, "")).strip() for name in KEY_NAMES}

@dataclass
class Config:
    workspace: str = field(default_factory=lambda: str(Path.home() / "Documents" / "LiteratureWorkflow"))
    school: str = ""
    email: str = ""
    access: str = "campus"
    institution_url: str = ""
    providers: list[str] = field(default_factory=lambda: ["openalex", "semantic_scholar"])
    home: Path = field(default_factory=default_home, repr=False)

    def __post_init__(self):
        self.home = Path(self.home).expanduser().resolve()
        self.workspace = str(Path(self.workspace).expanduser().resolve())

    @classmethod
    def load(cls, home=None):
        home = Path(home or default_home()).resolve()
        path = home / "config.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and k != "home"}, home=home)

    @property
    def credentials_path(self):
        return self.home / "credentials.env"

    def credentials(self):
        return read_credentials(self.credentials_path)

    def scan_config(self, save=False):
        from scanpdf.config import Config as ScanConfig
        config = ScanConfig.load(self.home / "downloader")
        for name in ("school", "email", "access", "institution_url"):
            setattr(config, name, getattr(self, name))
        config.output_dir = self.workspace
        if save:
            config.save()
        return config

    def validate(self, providers=None, credentials=False):
        self.scan_config().validate()
        # The original provenance archive nests three immutable UUIDs. Catch the
        # real Windows path limit before a search can fail after returning data.
        longest = Path(self.workspace) / "workspace/runs" / ("x" * 12) / "discovery/raw/semantic_scholar" / ("x" * 32) / ("x" * 32) / ("x" * 32 + ".json")
        if os.name == "nt" and len(str(longest).encode("utf-16-le")) // 2 > 245:
            raise ValueError("成果保存目录过长，请选择较短的位置，例如 D:\\Literature")
        names = self.providers if providers is None else providers
        if not names or set(names) - set(PROVIDER_KEYS):
            raise ValueError("检索来源请选择 openalex、scopus、wos、semantic_scholar、lens")
        if credentials:
            values = self.credentials()
            missing = [PROVIDER_KEYS[n] for n in names if n in REQUIRED_KEYS and not values[PROVIDER_KEYS[n]]]
            if missing:
                raise ValueError("启用的检索源尚缺凭据：" + ", ".join(missing))

    def save(self):
        self.validate()
        raw = asdict(self)
        raw.pop("home")
        atomic_json(self.home / "config.json", raw)
        self.scan_config(save=True)
        if not self.credentials_path.exists():
            self.credentials_path.write_text("# 只填写自己启用的服务；不要把此文件随 skill 分发。\n" +
                                             "".join(name + "=\n" for name in KEY_NAMES), encoding="utf-8")

    def activate(self):
        """Only this process and its children receive the user's settings."""
        values = self.credentials()
        for name, value in values.items():
            if value:
                os.environ[name] = value
        os.environ.setdefault("PAPER_CONTACT_EMAIL", self.email)
        os.environ.setdefault("UNPAYWALL_EMAIL", self.email)
        scan = self.scan_config()
        os.environ["SCANPDF_HOME"] = str(scan.home)
        os.environ["SCANSCI_PDF_DATA_DIR"] = str(scan.home / "scansci" / scan.school_id)
        os.environ["LITERATURE_WORKFLOW_HOME"] = str(self.home)

    def redact(self, text):
        for name, value in self.credentials().items():
            if value and ("KEY" in name or "TOKEN" in name):
                text = text.replace(value, "[REDACTED]")
        return text

def guide(config):
    values = config.credentials()
    return {"configured": bool(config.school and config.email), "workspace": config.workspace,
            "credential_file": str(config.credentials_path),
            "configured_credentials": {k: bool(v) for k, v in values.items()},
            "providers": config.providers,
            "steps": [
                "检查 Windows x64、Python 3.11、本机 Chrome 与固定版本浏览器内核（首次安装从官方源获取）。",
                "选择检索来源；逐个引导申请并在本机填写所需 API key，不要求一次拿齐所有服务。",
                "填写自己的学校全名、联系邮箱、校园网或学校官方机构入口，以及成果保存目录。",
                "保留 agent 通信与校园网分流；按需打开对应出版社，在本机完成学校认证。",
                "用学校有权访问的样本验证主 PDF 与附件，再启动正式检索。",
                "说明主题、年份和筛选条件；一个 run 自动检索、按清单下载、回填、归档和分类；中断用 resume。"],
            "next_action": "setup" if not config.school or not config.email else "doctor"}
