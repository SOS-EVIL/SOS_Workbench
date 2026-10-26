"""
core/api_key_manager.py - API Key 加密管理器（纯逻辑，零 UI 依赖）
使用 Fernet 对称加密，将 DeepSeek API Key 安全存储在本地文件。
"""

from pathlib import Path

try:
    from cryptography.fernet import Fernet
    HAS_FERNET = True
except ImportError:
    HAS_FERNET = False

class APIKeyManager:
    """负责 API Key 的加密存储和读取。"""

    def __init__(self, base_dir=None):
        """
        Args:
            base_dir: 项目根目录（可选，默认为本文件所在目录的上级目录）
        """
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        self.base_dir = Path(base_dir)
        self.key_path = self.base_dir / ".sos_api_key.enc"
        self.cipher = None
        self._init_cipher()

    def _init_cipher(self):
        """初始化加密器：从固定密钥文件读取密钥，若不存在则自动生成。"""
        if not HAS_FERNET:
            return
        cipher_key_path = self.base_dir / ".sos_cipher_key"
        if not cipher_key_path.exists():
            cipher_key = Fernet.generate_key()
            cipher_key_path.write_bytes(cipher_key)
        else:
            cipher_key = cipher_key_path.read_bytes()
        self.cipher = Fernet(cipher_key)

    def save(self, api_key):
        """加密并保存 API Key 到本地文件。"""
        if not self.cipher:
            return
        encrypted_key = self.cipher.encrypt(api_key.encode())
        self.key_path.write_bytes(encrypted_key)

    def load(self):
        """从本地文件读取并解密 API Key，若文件不存在或解密失败则返回 None。"""
        if not self.cipher or not self.key_path.exists():
            return None
        try:
            encrypted_key = self.key_path.read_bytes()
            return self.cipher.decrypt(encrypted_key).decode()
        except Exception:
            return None

    def mask_key(self, api_key):
        """将 API Key 用星号遮盖，仅保留长度。"""
        return "*" * len(api_key) if api_key else ""