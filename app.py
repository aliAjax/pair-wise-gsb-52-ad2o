"""应用入口：参数解析、依赖组装与HTTP服务生命周期。"""
import argparse
from pathlib import Path

from src.audit import AuditRecorder
from src.http_api import create_server
from src.recon_repository import ReconRepository
from src.recon_rules import ReconRules
from src.recon_service import ReconService
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "special-education.db"
DEFAULT_PORT = 8328


def build_service(db_path: str) -> Service:
    repository = Repository(db_path)
    audit = AuditRecorder(repository)
    return Service(repository, DomainRules(), audit)


def build_recon_service(db_path: str) -> ReconService:
    return ReconService(ReconRepository(db_path), ReconRules(), Repository(db_path))


def parse_args():
    parser = argparse.ArgumentParser(description="特殊教育支持计划合规")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite数据库路径")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    service = build_service(args.db)
    recon = build_recon_service(args.db)
    server = create_server(args.host, args.port, service, BASE_DIR / "static", recon)
    print("特殊教育支持计划合规 listening on http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
