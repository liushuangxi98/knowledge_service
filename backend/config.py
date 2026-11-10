from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', env_file_encoding='utf-8', extra='ignore')
    mysql_host: str = '127.0.0.1'
    mysql_port: int = 3306
    mysql_user: str = 'root'
    mysql_password: SecretStr = SecretStr('')
    mysql_database: str = Field(default='knowledge_service', pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    parse_batch_size: int = Field(gt=0, description='每批整理的文件数量')
    originals_dir: Path = ROOT / 'data' / 'originals'
    chroma_url: str = 'http://127.0.0.1:8100'
    chroma_dir: Path = ROOT / 'data' / 'chroma'

    def database_url(self, *, admin=False):
        return URL.create('mysql+pymysql', username=self.mysql_user, password=self.mysql_password.get_secret_value(),
                          host=self.mysql_host, port=self.mysql_port, database=None if admin else self.mysql_database,
                          query={'charset': 'utf8mb4'})
