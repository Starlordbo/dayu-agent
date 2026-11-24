"""组合材料字节轻量提取与财季规则，为上传确认前提供可编辑的预填结果。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dayu.fins.material_metadata_extraction import extract_material_metadata
from dayu.fins.upload_recognition import EarningsCallFiscalGuess, infer_earnings_call_fiscal


@dataclass(frozen=True)
class MaterialMetadataPrefill:
    """材料标题及保留声明来源、冲突和确认语义的财季推断。"""

    material_name: str
    fiscal: EarningsCallFiscalGuess


def prefill_material_metadata(*, filename: str, data: bytes) -> MaterialMetadataPrefill:
    """从已选文件预填标题和财季，不提交上传、转换或仓储操作。

    Args:
        filename: 原始文件名，作为轻量提取失败后的退回来源。
        data: 浏览器已持有的原始字节。
    Returns:
        可编辑的标题和既有规则推断；无法识别财季时保留未知与待确认状态。
    Raises:
        无；正文不可提取时仅使用文件名。
    """
    title, opening = extract_material_metadata(filename=filename, data=data)
    fiscal = infer_earnings_call_fiscal(filename=filename, title=title, opening=opening)
    return MaterialMetadataPrefill(
        material_name=title or Path(filename).stem.strip() or filename,
        fiscal=fiscal,
    )
