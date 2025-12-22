"""
Data Models for Sehwag Strategy
================================
Dataclasses and models for position tracking and state management.
Supports any index (NIFTY, SENSEX, BANKNIFTY, etc.).
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, Tuple, List
from datetime import datetime


@dataclass
class LegPosition:
    """Represents a single leg position in the strategy"""
    leg_id: int
    symbol: str
    entry_price: float
    entry_time: str
    quantity: int
    option_type: str  # "CE" or "PE"
    itm_level: int
    
    # Risk management
    current_sl: float
    lock_profit_pct: float = 2.0
    profit_level_for_lock_increase: float = 2.0
    
    # Position tracking
    current_ltp: float = 0.0
    is_active: bool = True
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    
    def calculate_pnl(self, current_price: float, entry_action: str = "BUY") -> Tuple[float, float]:
        """Calculate current PnL and PnL percentage"""
        if entry_action == "BUY":
            pnl = (current_price - self.entry_price) * self.quantity
            pnl_pct = ((current_price - self.entry_price) / self.entry_price) * 100
        else:
            pnl = (self.entry_price - current_price) * self.quantity
            pnl_pct = ((self.entry_price - current_price) / self.entry_price) * 100
        
        return pnl, pnl_pct


@dataclass
class StrategyState:
    """Track overall strategy state - supports dynamic number of legs"""
    entry_signal_active: bool = False
    entry_confirmed: bool = False
    entry_direction: Optional[str] = None  # "CE" or "PE"
    reference_price: float = 0.0  # Price at which we check 3% movement
    reference_time: Optional[str] = None
    
    # Dynamic leg tracking - dictionary indexed by leg number
    legs_entered: Dict[int, bool] = field(default_factory=dict)  # {1: True, 2: False, ...}
    leg_positions: Dict[int, Optional[LegPosition]] = field(default_factory=dict)  # {1: LegPosition(...), ...}
    leg_configs: Dict[int, Dict] = field(default_factory=dict)  # {1: {config}, ...}
    
    total_pnl: float = 0.0
    
    def get_position(self, leg_num: int) -> Optional[LegPosition]:
        """Get position for a specific leg"""
        return self.leg_positions.get(leg_num)
    
    def set_position(self, leg_num: int, position: LegPosition):
        """Set position for a specific leg"""
        self.leg_positions[leg_num] = position
        self.legs_entered[leg_num] = True
    
    def is_leg_entered(self, leg_num: int) -> bool:
        """Check if a leg has been entered"""
        return self.legs_entered.get(leg_num, False)
    
    def get_all_active_positions(self) -> List[Tuple[int, LegPosition]]:
        """Get all active positions as list of (leg_number, position) tuples"""
        return [(num, pos) for num, pos in self.leg_positions.items() 
                if pos and pos.is_active]
    
    def get_total_pnl(self) -> Tuple[float, float]:
        """Calculate total PnL across all positions"""
        total_pnl = 0.0
        total_pnl_pct = 0.0
        
        for position in self.leg_positions.values():
            if position and not position.is_active:
                total_pnl += position.pnl
                total_pnl_pct += position.pnl_pct
        
        return total_pnl, total_pnl_pct


@dataclass
class LegSchedule:
    """Represents the schedule for a leg entry and exit"""
    leg_num: int
    config: Dict
    entry_time: datetime
    exit_time: Optional[datetime] = None
    entered: bool = False
    
    @property
    def name(self) -> str:
        """Get leg name from config"""
        return self.config.get('name', f'Leg {self.leg_num}')

    @property
    def itm_level(self) -> int:
        """Get ITM level from config"""
        return self.config.get('itm_level', 3)
