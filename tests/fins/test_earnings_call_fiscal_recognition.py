"""电话会议纪要（EARNINGS_CALL）财季识别测试。

对应 `docs/p2-verification.md` 单元层 U1–U9。
"""

from __future__ import annotations

from typing import Optional

import pytest

from dayu.fins.upload_recognition import (
    REASON_CONFLICT,
    REASON_DECLARATION,
    REASON_NO_DECLARATION,
    REASON_Q4_AND_FY,
    EarningsCallFiscalGuess,
    infer_earnings_call_fiscal,
)


def _assert_guess(
    guess: EarningsCallFiscalGuess,
    *,
    year: Optional[int],
    period: Optional[str],
    source: Optional[str] = None,
    needs_confirmation: bool = False,
    conflict: bool = False,
    reason: str = REASON_DECLARATION,
) -> None:
    """统一断言财季推断结果。

    Args:
        guess: 待断言结果。
        year: 期望财年。
        period: 期望财季。
        source: 期望声明来源；`None` 表示不校验来源。
        needs_confirmation: 期望是否需要用户确认。
        conflict: 期望是否判定为来源冲突。
        reason: 期望判定原因。

    Returns:
        无。

    Raises:
        无。
    """
    assert (guess.fiscal_year, guess.fiscal_period) == (year, period)
    assert guess.needs_confirmation is needs_confirmation
    assert guess.conflict is conflict
    assert guess.reason == reason
    if source is not None:
        assert guess.source == source


def test_u1_filename_declares_q4() -> None:
    """U1：文件名声明 Q4 与年份。"""
    guess = infer_earnings_call_fiscal(filename="AMZN_Q4_2023_earnings_call.txt")
    _assert_guess(guess, year=2023, period="Q4", source="filename")


def test_u2_title_q4_and_full_year_stays_q4() -> None:
    """U2：标题同时出现 Fourth Quarter 与 Full Year 时保留 Q4。"""
    guess = infer_earnings_call_fiscal(
        filename="2023-transcript.txt",
        title="Amazon.com Announces Fourth Quarter and Full Year 2023 Results",
    )
    _assert_guess(guess, year=2023, period="Q4", source="title", reason=REASON_Q4_AND_FY)


def test_u3_q4_and_fy_single_material() -> None:
    """U3：一份“Q4 及全年”纪要只产出一个财季判断。"""
    guess = infer_earnings_call_fiscal(title="Q4 and Full Year 2023 Earnings Call Transcript")
    _assert_guess(guess, year=2023, period="Q4", reason=REASON_Q4_AND_FY)


def test_u4_outlook_does_not_change_period() -> None:
    """U4：展望年份不改变所属财季。"""
    guess = infer_earnings_call_fiscal(
        filename="FY2022_earnings_call.md",
        opening=(
            "We are pleased to report full year 2022 results. "
            "Looking ahead to 2023, we expect continued growth."
        ),
    )
    _assert_guess(guess, year=2022, period="FY", source="opening")


def test_u5_non_calendar_fiscal_year_uses_disclosure() -> None:
    """U5：非自然财年按披露口径取年份，不用发布日推断。"""
    guess = infer_earnings_call_fiscal(
        filename="2024-03-15-earnings-call.txt",
        title="Fiscal Year Ended January 31, 2023 Results",
    )
    _assert_guess(guess, year=2023, period="FY", source="title")


def test_u6_missing_declaration_requires_confirmation() -> None:
    """U6：没有明确声明时不猜，要求用户确认。"""
    guess = infer_earnings_call_fiscal(
        filename="earnings-call.txt",
        title="Earnings Call Transcript",
        opening="Good afternoon, everyone. Thank you for joining.",
    )
    _assert_guess(
        guess,
        year=None,
        period=None,
        source=None,
        needs_confirmation=True,
        reason=REASON_NO_DECLARATION,
    )


def test_u7_conflicting_sources_keep_higher_priority() -> None:
    """U7：文件名与标题财季冲突时取更高优先级来源并要求确认。"""
    guess = infer_earnings_call_fiscal(
        filename="Q3-2023-call.txt",
        title="Q4 2023 Earnings Call",
    )
    _assert_guess(
        guess,
        year=2023,
        period="Q4",
        source="title",
        needs_confirmation=True,
        conflict=True,
        reason=REASON_CONFLICT,
    )


def test_u8_comparative_year_is_ignored() -> None:
    """U9：对比句中的历史年份不参与归属。"""
    guess = infer_earnings_call_fiscal(
        opening="Fourth quarter 2023 results compared with Q4 2022.",
    )
    _assert_guess(guess, year=2023, period="Q4", source="opening")


def test_fy_only_is_not_upgraded_from_quarter() -> None:
    """回归：只有全年声明时保持 FY，不被误判成 Q4。"""
    guess = infer_earnings_call_fiscal(title="Full Year 2023 Results")
    _assert_guess(guess, year=2023, period="FY", source="title")


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_inputs_are_treated_as_no_declaration(blank: Optional[str]) -> None:
    """全空输入按无声明处理。"""
    guess = infer_earnings_call_fiscal(filename=blank, title=blank, opening=blank)
    assert guess.fiscal_year is None
    assert guess.fiscal_period is None
    assert guess.needs_confirmation is True
    assert guess.reason == REASON_NO_DECLARATION


def test_intra_source_multiple_quarters_require_confirmation() -> None:
    """同一标题出现两个不同季度时不得静默取第一个。"""
    guess = infer_earnings_call_fiscal(title="Q4 2023 and Q2 2024 Results")
    _assert_guess(
        guess,
        year=2023,
        period="Q4",
        source="title",
        needs_confirmation=True,
        conflict=True,
        reason=REASON_CONFLICT,
    )


def test_full_year_with_quarter_details_keeps_fy() -> None:
    """“全年为主、季度为细节”的标题保留 FY。"""
    guess = infer_earnings_call_fiscal(title="Full Year 2023 with Q4 details")
    _assert_guess(guess, year=2023, period="FY", source="title", reason=REASON_Q4_AND_FY)


def test_chinese_comparison_clause_is_ignored() -> None:
    """中文对比分句不改变当期财季与年份。"""
    guess = infer_earnings_call_fiscal(opening="与2022年Q4相比，2023年Q4收入增长明显。")
    _assert_guess(guess, year=2023, period="Q4", source="opening")


def test_leading_comparison_clause_keeps_main_clause() -> None:
    """前置英文对比从句只删到逗号，不吞掉主句。"""
    guess = infer_earnings_call_fiscal(opening="Compared with 2022, Q4 2023 revenue grew.")
    _assert_guess(guess, year=2023, period="Q4", source="opening")


def test_publication_date_in_filename_does_not_conflict() -> None:
    """文件名里的发布日期不能顶掉标题里的财年。"""
    guess = infer_earnings_call_fiscal(
        filename="2024-02-01_Q4_2023.txt",
        title="Q4 2023 Earnings Call",
    )
    _assert_guess(guess, year=2023, period="Q4", source="title")


def test_nearest_year_wins_over_leftmost_year() -> None:
    """跨公司引用时按距离取年份，不取最左年份。"""
    guess = infer_earnings_call_fiscal(title="乙公司 2022；甲公司 Q4 2023 电话会")
    _assert_guess(guess, year=2023, period="Q4", source="title")
