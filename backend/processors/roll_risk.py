"""滚仓风险数学引擎 —— 纯函数，无外部状态

职责：
  - 加权均价 / 有效杠杆 / 浮盈 / 爆仓价 / 止损 的数学计算
  - 加仓量计算（4 种 AddMode 策略）
  - 三道安全闸门检查 + 二分法缩量求解
  - 加仓前后指标对比（构建 AddPreview 的辅助）

设计约定：
  - 所有函数为纯函数（无 I/O，无 side effect），便于单测和并发
  - 所有金额以 USD 计价，所有比例显式用 pct（0~100）或 ratio（0~1）
  - long/short 统一处理：方向通过 side: Side 参数传入
  - isolated/cross 统一处理：margin_mode 传入，爆仓公式分支
  - 维持保证金率（mmr）默认 0.005（0.5%），参数化便于未来调校

输入字段对齐约定（重要）：
  - `margin_used_usd`：用户实际投入的保证金（user-declared, 不含浮盈）
  - `position_size`：币数（同方向累计）
  - `entry_price`：加权均价
  - `current_price`：评估时的市场价
  - 浮盈由 `current_price` 与 `entry_price` 差计算得出，不入 margin_used
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from models.roll_position import (
    AddIntensity,
    AddMode,
    MarginMode,
    RollPlan,
    SafetyGates,
    Side,
    UserPosition,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局默认参数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 维持保证金率（OKX USDT 永续典型 0.5%）
# 实际 OKX 按仓位阶梯变化（0.5% / 1% / 1.5%），这里取保守平均
DEFAULT_MAINTENANCE_MARGIN_RATIO = 0.005

# 加仓烈度对应的量乘数（full/half/small/reject）—— 对应方案表格
INTENSITY_MULTIPLIER: dict[AddIntensity, float] = {
    "full": 1.0,
    "half": 0.5,
    "small": 0.3,
    "reject": 0.0,
}

# 加仓烈度对应的闸门覆盖（small 档闸门更紧）
# 若策略模板中的 gates.min_avg_distance_pct 比 small 档要求更紧，取更紧的
INTENSITY_GATE_OVERRIDE: dict[AddIntensity, dict] = {
    "full":  {"min_avg_distance_pct": 2.5},
    "half":  {"min_avg_distance_pct": 3.0},
    "small": {"min_avg_distance_pct": 5.0},
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 基础指标：浮盈 / 有效杠杆
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def unrealized_pnl_usd(
    side: Side,
    entry_price: float,
    current_price: float,
    position_size: float,
) -> float:
    """未实现盈亏（USD）。

    long  : size × (current - entry)
    short : size × (entry - current)
    """
    if position_size <= 0 or entry_price <= 0 or current_price <= 0:
        return 0.0
    if side == "long":
        return position_size * (current_price - entry_price)
    return position_size * (entry_price - current_price)


def unrealized_pnl_pct(
    side: Side,
    entry_price: float,
    current_price: float,
    leverage: int,
) -> float:
    """未实现盈亏 / 保证金 的百分比（相对于保证金的收益率，带杠杆放大）。

    例：10x 空单，价格下跌 5%，本函数返回 50.0。
    """
    if entry_price <= 0 or current_price <= 0 or leverage <= 0:
        return 0.0
    price_move_ratio = (
        (current_price - entry_price) / entry_price
        if side == "long"
        else (entry_price - current_price) / entry_price
    )
    return price_move_ratio * leverage * 100.0


def effective_leverage(
    position_size: float,
    current_price: float,
    margin_used_usd: float,
    unrealized_pnl: float,
) -> float:
    """有效杠杆 = 当前名义价值 / (占用保证金 + 浮盈)

    说明：
      - 浮盈加入分母，反映"赚到的钱开始起到保证金作用"，杠杆会自动被动下降
      - 若分母 <= 0（极端浮亏），返回 999.0 作为异常标记（距离爆仓极近）
    """
    notional = position_size * current_price
    denom = margin_used_usd + unrealized_pnl
    if denom <= 0:
        return 999.0
    return notional / denom


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加权均价
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def weighted_avg_entry(
    old_size: float,
    old_entry: float,
    add_size: float,
    add_price: float,
) -> float:
    """加仓后的加权均价。

    (old_size × old_entry + add_size × add_price) / (old_size + add_size)

    注意：本函数对 long/short 都适用（方向不影响均价计算公式，
    方向只影响盈亏方向和爆仓方向）。
    """
    total_size = old_size + add_size
    if total_size <= 0:
        return 0.0
    return (old_size * old_entry + add_size * add_price) / total_size


def size_from_margin(margin_usd: float, leverage: int, price: float) -> float:
    """给定保证金 × 杠杆 ÷ 价格 → 持币数量。"""
    if price <= 0 or leverage <= 0:
        return 0.0
    return margin_usd * leverage / price


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 爆仓价估算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def estimate_liq_price(
    side: Side,
    margin_mode: MarginMode,
    entry_price: float,
    leverage: int,
    position_size: float,
    margin_used_usd: float,
    total_account_usd: Optional[float] = None,
    mmr: float = DEFAULT_MAINTENANCE_MARGIN_RATIO,
) -> Optional[float]:
    """估算爆仓价。

    逐仓（isolated）：
      long  : liq = entry × (1 - 1/leverage) / (1 - mmr)
      short : liq = entry × (1 + 1/leverage) / (1 + mmr)

    全仓（cross）：
      把 total_account_usd 当作可用保证金（简化）：
        可承受亏损 = total_account_usd - position_size × entry × mmr
        long  : liq = entry - (total_account_usd / position_size) × (1 - mmr 逼近)
      为避免误导，cross 返回保守估算，UI 需明确标注"仅供参考"。

    参数异常时返回 None（不编造）。
    """
    if entry_price <= 0 or leverage <= 0 or position_size <= 0:
        return None
    if margin_mode == "isolated":
        if margin_used_usd <= 0:
            return None
        if side == "long":
            return entry_price * (1 - 1 / leverage) / (1 - mmr)
        return entry_price * (1 + 1 / leverage) / (1 + mmr)

    # cross 模式：简化估算
    if total_account_usd is None or total_account_usd <= 0:
        return None
    # 账户能承担的最大亏损（不考虑其他持仓占用）
    max_loss = total_account_usd - position_size * entry_price * mmr
    if max_loss <= 0:
        return None
    # long: entry - liq = max_loss / size
    if side == "long":
        return max(0.0, entry_price - max_loss / position_size)
    return entry_price + max_loss / position_size


def distance_pct(from_price: float, to_price: float) -> float:
    """(to - from) / from × 100，带符号。"""
    if from_price <= 0:
        return 0.0
    return (to_price - from_price) / from_price * 100.0


def abs_distance_pct(a: float, b: float) -> float:
    """|a - b| / a × 100，始终非负。"""
    if a <= 0:
        return 0.0
    return abs(a - b) / a * 100.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓量计算（4 种策略）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class IdealAddContext:
    """compute_ideal_add_margin 的上下文输入。"""

    position: UserPosition
    plan: RollPlan
    current_price: float
    past_add_count: int                 # 本计划已发生的 add 事件数（用于 pyramid 递减）
    first_add_ratio: float = 0.5        # pyramid 首次加仓占初始保证金的比例
    reinvest_ratio: float = 1.0         # passive 浮盈再投入比例（1.0=全部浮盈再投入）


def compute_ideal_add_margin(ctx: IdealAddContext) -> float:
    """按策略模式计算"理论加仓保证金"（未经烈度乘数和闸门缩量）。

    四种模式的语义：
      passive_deleveraging:
        浮盈再投入 = unrealized_pnl × reinvest_ratio
        这是"肥仔派"的实际操作数学：把赚到的钱作为新加仓的保证金。

      pyramid_decay:
        m(n) = initial_margin × first_add_ratio × decay_ratio^(past_add_count)
        越加越少，闸门自然不易触发。

      layered_independent:
        m = total_account × layered_pct_of_account
        每次独立，与现有仓位无关（李法师派）。

      fixed_ratio:
        m = 当前保证金 × fixed_ratio_of_position
        等比例加，用户最需谨慎（默认值锁在 ≤ 0.3 避免均价压缩过快）。
    """
    pos = ctx.position
    plan = ctx.plan
    mode: AddMode = plan.add_mode

    if mode == "passive_deleveraging":
        pnl = unrealized_pnl_usd(
            pos.side, pos.entry_price, ctx.current_price, pos.position_size
        )
        if pnl <= 0:
            return 0.0
        return pnl * ctx.reinvest_ratio

    if mode == "pyramid_decay":
        # 以"首次加仓投入 = 初始保证金 × first_add_ratio"为基准递减
        # past_add_count 从 0 开始：第 1 次加仓 past_add_count=0
        initial_margin = _find_initial_margin(pos)
        base = initial_margin * ctx.first_add_ratio
        return base * (plan.pyramid_decay_ratio ** ctx.past_add_count)

    if mode == "layered_independent":
        return pos.total_account_usd * plan.layered_pct_of_account

    if mode == "fixed_ratio":
        return pos.margin_used_usd * plan.fixed_ratio_of_position

    return 0.0


def _find_initial_margin(position: UserPosition) -> float:
    """从事件链中找初始保证金（init 事件的 margin_delta_usd）。
    若 events 空或没有 init 事件，回退用当前 margin_used_usd。
    """
    for ev in position.events:
        if ev.kind == "init":
            return ev.margin_delta_usd if ev.margin_delta_usd > 0 else position.margin_used_usd
    return position.margin_used_usd


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓后指标模拟（构建 AddPreview 数据）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SimulatedMetrics:
    """加仓后的预演指标集（与 AddPreview.PreviewMetrics 对齐）。"""

    avg_price: float
    distance_to_price_pct: float       # 均价 vs 现价（带符号：负=有浮盈）
    effective_leverage: float
    liq_price: Optional[float]
    liq_distance_pct: Optional[float]  # 现价 vs 爆仓（正=安全距离）
    position_value_usd: float
    margin_used_usd: float
    account_margin_pct: float          # 占账户百分比


def simulate_after_add(
    position: UserPosition,
    add_margin_usd: float,
    current_price: float,
    mmr: float = DEFAULT_MAINTENANCE_MARGIN_RATIO,
) -> SimulatedMetrics:
    """模拟加仓 add_margin_usd 后的关键指标（用于闸门检查 + 预演）。

    add_margin_usd=0 时返回加仓前的指标（便于 before/after 对比统一）。
    """
    leverage = position.leverage
    size_add = size_from_margin(add_margin_usd, leverage, current_price)
    new_size = position.position_size + size_add
    new_margin = position.margin_used_usd + add_margin_usd

    if add_margin_usd > 0:
        new_avg = weighted_avg_entry(
            position.position_size, position.entry_price,
            size_add, current_price,
        )
    else:
        new_avg = position.entry_price

    new_pnl = unrealized_pnl_usd(position.side, new_avg, current_price, new_size)
    new_notional = new_size * current_price
    new_eff_lev = effective_leverage(new_size, current_price, new_margin, new_pnl)

    new_liq = estimate_liq_price(
        side=position.side,
        margin_mode=position.margin_mode,
        entry_price=new_avg,
        leverage=leverage,
        position_size=new_size,
        margin_used_usd=new_margin,
        total_account_usd=position.total_account_usd,
        mmr=mmr,
    )

    liq_dist_pct: Optional[float] = None
    if new_liq is not None and new_liq > 0:
        # 距离爆仓（正=现价离爆仓远 = 安全）
        if position.side == "long":
            liq_dist_pct = (current_price - new_liq) / current_price * 100.0
        else:
            liq_dist_pct = (new_liq - current_price) / current_price * 100.0

    # 均价距现价百分比（带符号，符号约定见下）
    # long 持仓：浮盈时 current > entry → distance_to_price_pct 正（现价在均价上方）
    # short 持仓：浮盈时 current < entry → distance_to_price_pct 负
    # 统一用 (current - avg) / avg × 100（long/short 语义相反，前端按 side 解释）
    price_dist_pct = (current_price - new_avg) / new_avg * 100.0 if new_avg > 0 else 0.0

    acct_pct = (
        new_margin / position.total_account_usd * 100.0
        if position.total_account_usd > 0 else 0.0
    )

    return SimulatedMetrics(
        avg_price=new_avg,
        distance_to_price_pct=price_dist_pct,
        effective_leverage=new_eff_lev,
        liq_price=new_liq,
        liq_distance_pct=liq_dist_pct,
        position_value_usd=new_notional,
        margin_used_usd=new_margin,
        account_margin_pct=acct_pct,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 三道闸门检查
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class GateCheckResult:
    """闸门检查结果（对齐 RollSignal.GatesStatus）。"""

    gate_a_pass: bool
    gate_b_pass: bool
    gate_c_pass: bool

    gate_a_actual: float                # 均价距现价 |%|
    gate_a_required: float
    gate_b_actual: float                # 爆仓距现价 %
    gate_b_required: float
    gate_c_actual: float                # 有效杠杆
    gate_c_required: float

    @property
    def all_pass(self) -> bool:
        return self.gate_a_pass and self.gate_b_pass and self.gate_c_pass

    def failing_reasons(self) -> list[str]:
        reasons = []
        if not self.gate_a_pass:
            reasons.append(
                f"闸门A(均价距现价 {self.gate_a_actual:.2f}% < {self.gate_a_required:.2f}%)"
            )
        if not self.gate_b_pass:
            reasons.append(
                f"闸门B(爆仓距现价 {self.gate_b_actual:.2f}% < {self.gate_b_required:.2f}%)"
            )
        if not self.gate_c_pass:
            reasons.append(
                f"闸门C(有效杠杆 {self.gate_c_actual:.2f} > {self.gate_c_required:.2f})"
            )
        return reasons


def check_gates(
    metrics: SimulatedMetrics,
    gates: SafetyGates,
    intensity: AddIntensity = "full",
) -> GateCheckResult:
    """检查三道闸门 —— 根据加仓烈度应用更紧的阈值。

    small 档：均价距现价闸门强制升到 5%（即便模板设的更松）。
    half 档：升到 3%。
    full 档：使用模板值。
    """
    # 应用烈度覆盖（只收紧不放松）
    override = INTENSITY_GATE_OVERRIDE.get(intensity, {})
    min_avg_req = max(
        gates.min_avg_distance_pct,
        override.get("min_avg_distance_pct", 0.0),
    )

    # 闸门 A · 均价距现价（取绝对值，长短仓对称）
    gate_a_actual = abs(metrics.distance_to_price_pct)
    gate_a_pass = gate_a_actual >= min_avg_req

    # 闸门 B · 爆仓距现价（取正向距离 = 安全距离）
    gate_b_actual = metrics.liq_distance_pct if metrics.liq_distance_pct is not None else 0.0
    gate_b_pass = gate_b_actual >= gates.min_liq_distance_pct

    # 闸门 C · 有效杠杆
    gate_c_actual = metrics.effective_leverage
    gate_c_pass = gate_c_actual <= gates.max_eff_leverage

    return GateCheckResult(
        gate_a_pass=gate_a_pass,
        gate_b_pass=gate_b_pass,
        gate_c_pass=gate_c_pass,
        gate_a_actual=gate_a_actual,
        gate_a_required=min_avg_req,
        gate_b_actual=gate_b_actual,
        gate_b_required=gates.min_liq_distance_pct,
        gate_c_actual=gate_c_actual,
        gate_c_required=gates.max_eff_leverage,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 二分法求解：闸门下的最大安全加仓量
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SafeAddResult:
    """二分求解输出。"""

    final_margin_usd: float             # 最终建议加仓量（可能被缩到 0）
    gates: GateCheckResult              # 最终量对应的闸门状态
    shrink_reason: str = ""             # 若缩量，说明主要约束来源
    accepted: bool = True               # 若最终量 < min_add_margin_usd 则 False


def binary_search_safe_margin(
    position: UserPosition,
    ideal_margin_usd: float,
    current_price: float,
    gates: SafetyGates,
    intensity: AddIntensity = "full",
    mmr: float = DEFAULT_MAINTENANCE_MARGIN_RATIO,
    max_iter: int = 24,
    tol_usd: float = 0.5,
) -> SafeAddResult:
    """二分法求"最大满足三道闸门的加仓量"（范围 [0, ideal]）。

    算法：
      - 若 ideal=0 → 直接返回
      - 若 ideal 就能过 → 不缩量
      - 若 ideal 不能过 → 二分缩到恰好过闸门的点
      - 若 0 都不能过闸门 C（说明当前已超杠杆上限） → 返回 accepted=False

    闸门 C 是单调的（加仓只会让 eff_lev 上升 或 下降 —— 对浮盈下"下降"）。
    但为保守处理，我们对所有闸门都用二分（闸门 B 同理，加仓后爆仓更近）。
    """
    if ideal_margin_usd <= 0:
        base = simulate_after_add(position, 0, current_price, mmr)
        return SafeAddResult(
            final_margin_usd=0.0,
            gates=check_gates(base, gates, intensity),
            shrink_reason="理论加仓量为 0",
            accepted=False,
        )

    # 先试 ideal
    sim_ideal = simulate_after_add(position, ideal_margin_usd, current_price, mmr)
    gates_ideal = check_gates(sim_ideal, gates, intensity)
    if gates_ideal.all_pass:
        return SafeAddResult(
            final_margin_usd=ideal_margin_usd,
            gates=gates_ideal,
            shrink_reason="",
            accepted=True,
        )

    # 试 0（下界）—— 加仓 0 时闸门 A 必然不过（无均价变化）
    # 但闸门 B/C 可能不过（说明当前持仓本身已越限）
    sim_zero = simulate_after_add(position, 0, current_price, mmr)
    gates_zero = check_gates(sim_zero, gates, intensity)

    # 闸门 A 的语义：加仓后均价距现价不得过近；不加仓时"加仓后 == 不变 == 过关"？
    # 但我们用"加仓需体现均价变动"的数学：加仓量为 0 → 均价不变 → 距现价等于当前持仓的距离
    # 此时若当前距离 < 要求 → 闸门 A 不过 → 但这不是"加仓导致"的，是持仓本身的状态
    # 处理原则：若 0 加仓时 A 已不过 → 0 加仓不算"危险动作"，但加仓也不被允许
    #          直接判定 accepted=False，shrink_reason 指向 A

    # 二分：若 0 都不过 → 没救
    if not gates_zero.gate_c_pass:
        return SafeAddResult(
            final_margin_usd=0.0,
            gates=gates_zero,
            shrink_reason="当前有效杠杆已超上限，不能加仓",
            accepted=False,
        )

    lo, hi = 0.0, ideal_margin_usd
    best_margin = 0.0
    best_gates = gates_zero

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        sim_mid = simulate_after_add(position, mid, current_price, mmr)
        gates_mid = check_gates(sim_mid, gates, intensity)
        if gates_mid.all_pass:
            best_margin = mid
            best_gates = gates_mid
            lo = mid                        # 试更大
        else:
            hi = mid                        # 缩小

        if hi - lo < tol_usd:
            break

    accepted = best_margin >= gates.min_add_margin_usd
    reason = ""
    if best_margin < ideal_margin_usd:
        # 找出最先被触发的闸门作为主因
        sim_ideal_check = gates_ideal
        if not sim_ideal_check.gate_c_pass:
            reason = "闸门C(有效杠杆)"
        elif not sim_ideal_check.gate_b_pass:
            reason = "闸门B(爆仓距离)"
        elif not sim_ideal_check.gate_a_pass:
            reason = "闸门A(均价距离)"

    if not accepted:
        reason = reason or "闸门约束下的安全加仓量低于最小值"

    return SafeAddResult(
        final_margin_usd=best_margin if accepted else 0.0,
        gates=best_gates,
        shrink_reason=reason,
        accepted=accepted,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 止损移动建议
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_trail_sl(
    position: UserPosition,
    current_price: float,
    atr: float,
    past_add_count: int,
    plan: RollPlan,
) -> Optional[float]:
    """计算建议的新止损位。

    规则：
      - past_add_count == plan.trail_sl_after_add_n → 挪到保本价（initial 事件的均价）
      - past_add_count > 此值 → 挪到"上次加仓价 ± trail_sl_atr_mult × ATR"

    返回 None 表示"无需移动"（新止损不优于当前止损）。
    """
    if past_add_count < plan.trail_sl_after_add_n:
        return None

    current_sl = position.stop_loss

    # 第 N 次加仓后：挪到保本价（= initial entry，用 initial_stop_loss 的对面 = entry）
    # 规则：保本 = position.events 中 init 事件的 price（即首次开仓均价）
    initial_entry = _find_initial_entry_price(position)
    atr_offset = atr * plan.trail_sl_atr_mult if atr > 0 else 0.0

    if past_add_count == plan.trail_sl_after_add_n:
        candidate = initial_entry
    else:
        # 更深层滚仓：参考上次加仓价
        last_add_price = _find_last_add_price(position)
        if last_add_price <= 0:
            candidate = initial_entry
        else:
            if position.side == "long":
                candidate = last_add_price - atr_offset
            else:
                candidate = last_add_price + atr_offset

    # 只允许单向移动（long 止损只能上移，short 止损只能下移）
    if current_sl is not None:
        if position.side == "long" and candidate <= current_sl:
            return None
        if position.side == "short" and candidate >= current_sl:
            return None

    return candidate


def _find_initial_entry_price(position: UserPosition) -> float:
    for ev in position.events:
        if ev.kind == "init" and ev.price > 0:
            return ev.price
    return position.entry_price  # 回退


def _find_last_add_price(position: UserPosition) -> float:
    for ev in reversed(position.events):
        if ev.kind == "add" and ev.price > 0:
            return ev.price
    return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 置信度分数 → 加仓烈度 映射
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def intensity_from_score(score: float, plan: RollPlan) -> AddIntensity:
    """把累加 confidence_score 映射到加仓烈度。

    使用策略模板中的阈值。阈值范围校验在 roll_templates.py 完成。
    """
    t = plan.thresholds
    if score >= t.full_add:
        return "full"
    if score >= t.half_add:
        return "half"
    if score >= t.small_add:
        return "small"
    return "reject"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助：初始保证金 / 距上次加仓的 ATR 距离
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def count_add_events(position: UserPosition) -> int:
    """统计本持仓已发生的 add 事件数（不含 user_override_add，后者单独）。"""
    return sum(1 for ev in position.events if ev.kind == "add")


def count_user_override_events(position: UserPosition) -> int:
    return sum(1 for ev in position.events if ev.kind == "user_override_add")


def bars_since_last_add_in_atr(
    position: UserPosition,
    current_price: float,
    atr: float,
) -> float:
    """上次加仓到现在价格走了几个 ATR。

    若从未加过仓 → 返回 +inf（等同于"间距足够"）
    """
    if atr <= 0:
        return 0.0
    last_price = _find_last_add_price(position)
    if last_price <= 0:
        return float("inf")
    return abs(current_price - last_price) / atr
