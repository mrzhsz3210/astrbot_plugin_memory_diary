# -*- coding: utf-8 -*-
"""领域异常：存储层通过它们表达 fail-closed 的拒绝原因。"""

class DiaryKeyUnavailable(Exception):
    """签名密钥不可用（fail-closed）：拒绝写入与验签，绝不退回公开常量。"""


class DiaryTamperedError(Exception):
    """保存前验签发现既有日记被篡改/签名缺失：整次保存被拒绝。"""


class DiaryCorruptError(Exception):
    """日记 zip 损坏：已隔离备份，在人工处理前拒绝覆盖写入。"""


class DiaryDecryptionError(Exception):
    """密文认证失败、口令错误或格式不支持；不得按 ZIP 损坏隔离。"""


class DiaryPassphraseRequired(DiaryDecryptionError):
    """读取或开启加密所需口令缺失/长度不足。"""
