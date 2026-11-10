"""
模块名称:  config.py
所属项目:  knowledge_service
创建时间:  2026/11/9 02:12
作者:      刘双喜 <1604030114@qq.com>
功能描述:
"""
import hashlib
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# 从本文件位置往上两级定位项目根目录，避免受启动目录影响。
ROOT = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / '.env', env_file_encoding='utf-8', extra='ignore', str_strip_whitespace=True
    )
    llm_model: str = Field(title='语言模型', default='')
    llm_api_key: SecretStr = Field(title='语言模型密钥', default=SecretStr(''))
    llm_base_url: str = Field(title='模型服务地址', default='')

    embedding_model: str = Field(title='嵌入式模型', default='')
    embedding_api_key: SecretStr = Field(title='嵌入式模型密钥', default=SecretStr(''))
    embedding_base_url: str = Field(title='嵌入式服务地址', default='')

    chroma_dir: Path = ROOT / 'data' / 'chroma'

    def get_chroma_database_version(self):
        identity = self.embedding_model + ':' + self.embedding_base_url
        return hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]