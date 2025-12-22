"""
Sehwag Persistence Manager
===========================
Handles database persistence for the Sehwag strategy (supports any index).
Includes crash detection and recovery capabilities.
"""

import logging
from datetime import datetime
from typing import Dict, List
import uuid

from .sehwag_db import (
    create_session, update_session_status,
    log_position, update_position_status,
    log_order, update_order_status,
    log_event, db_session, SehwagSession, SehwagPosition
)

logger = logging.getLogger(__name__)


class SehwagPersistence:
    """Manages database persistence for Sehwag strategy (supports any index)"""

    def __init__(self, expiry_date: str, index_symbol: str, strike_diff: int, lot_size: int):
        """
        Initialize persistence manager
        
        Args:
            expiry_date: Expiry date for the session (YYYY-MM-DD)
            index_symbol: Index being traded (NIFTY, SENSEX, BANKNIFTY, etc.)
            strike_diff: Strike difference for the index
            lot_size: Lot size for the index
        """
        self.session_id = str(uuid.uuid4())
        self.expiry_date = expiry_date
        self.index_symbol = index_symbol
        self.strike_diff = strike_diff
        self.lot_size = lot_size
        self.leg_positions = {}  # {leg_num: position_id}
        
        # Create database session
        session = create_session(
            session_id=self.session_id,
            expiry_date=expiry_date,
            index_symbol=index_symbol,
            strike_diff=strike_diff,
            lot_size=lot_size,
            notes=f"Sehwag Strategy Session - {index_symbol}"
        )
        
        if session:
            logger.info(f"✓ Persistence initialized - Session ID: {self.session_id}")
            self.log_event("STRATEGY_START", "Strategy session started")
        else:
            logger.warning("⚠️  Failed to create database session")
    
    def log_event(self, event_type: str, description: str, metadata: Dict = None):
        """Log a strategy event"""
        try:
            log_event(
                session_id=self.session_id,
                event_type=event_type,
                description=description,
                metadata=metadata
            )
        except Exception as e:
            logger.error(f"Error logging event: {e}")
    
    def log_entry_condition(self, direction: str, highest_high: float, lowest_low: float, 
                           current_price: float, met: bool):
        """Log entry condition check"""
        self.log_event(
            "ENTRY_CONDITION_MET" if met else "ENTRY_CONDITION_CHECKED",
            f"Entry condition {'met' if met else 'not met'}: {direction if met else 'none'}",
            metadata={
                'direction': direction,
                'highest_high': highest_high,
                'lowest_low': lowest_low,
                'current_price': current_price,
                'met': met
            }
        )
    
    def log_wait_trade(self, success: bool, reference_price: float, threshold_pct: float):
        """Log Wait & Trade confirmation"""
        self.log_event(
            "WAIT_TRADE_CONFIRMED" if success else "WAIT_TRADE_FAILED",
            f"Wait & Trade {'confirmed' if success else 'failed'} ({threshold_pct}%)",
            metadata={
                'reference_price': reference_price,
                'threshold_pct': threshold_pct,
                'success': success
            }
        )
    
    def log_leg_entry(self, leg_num: int, leg_name: str, symbol: str, entry_price: float,
                      quantity: int, option_type: str, strike: float, initial_sl: float,
                      atm_strike: float = None):
        """Log leg entry"""
        # Use atm_strike or strike as fallback
        atm_strike_value = int(atm_strike) if atm_strike else int(strike)

        position_id = log_position(
            session_id=self.session_id,
            leg_number=leg_num,
            symbol=symbol,
            atm_strike=atm_strike_value,
            strike=int(strike),
            option_type=option_type,
            entry_price=entry_price,
            quantity=quantity,
            initial_sl=initial_sl
        )
        
        if position_id:
            self.leg_positions[leg_num] = position_id
            logger.info(f"✓ Logged {leg_name} entry - Position ID: {position_id}")
        
        return position_id
    
    def log_order(self, leg_num: int, order_type: str, symbol: str, quantity: int,
                  price: float, side: str = "BUY", notes: str = None):
        """Log order placement"""
        return log_order(
            session_id=self.session_id,
            order_type=order_type,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            notes=notes or f"Leg {leg_num} {order_type}"
        )
    
    def update_order_status(self, order_db_id: int, status: str, executed_price: float = None):
        """Update order status"""
        update_order_status(order_db_id, status, executed_price)
    
    def update_position(self, leg_num: int, current_price: float, current_sl: float,
                       lock_profit_pct: float, profit_pct: float):
        """Update position with current values"""
        position_id = self.leg_positions.get(leg_num)
        if position_id:
            try:
                from .sehwag_db import db_session, SehwagPosition
                position = db_session.query(SehwagPosition).filter_by(id=position_id).first()
                if position:
                    position.current_price = current_price
                    position.current_sl = current_sl
                    position.lock_profit_pct = lock_profit_pct
                    position.profit_pct = profit_pct
                    position.updated_at = datetime.now()
                    db_session.commit()
            except Exception as e:
                logger.error(f"Error updating position: {e}")
    
    def record_leg_entry(self, leg_num: int, leg_name: str, symbol: str,
                        entry_price: float, quantity: int, initial_sl: float):
        """
        Convenience wrapper for recording leg entry (simplified version)

        Args:
            leg_num: Leg number
            leg_name: Leg name
            symbol: Option symbol
            entry_price: Entry price
            quantity: Quantity
            initial_sl: Initial stop loss
        """
        # Extract option type and strike from symbol (e.g., NIFTY24DEC20500CE)
        option_type = "CE" if "CE" in symbol else "PE"

        # Try to extract strike from symbol
        try:
            # Find the position of CE/PE in symbol
            pos = symbol.find(option_type)
            # Extract numbers before CE/PE
            strike_str = symbol[:pos]
            # Get last 5 digits as strike (e.g., 20500)
            strike = float(strike_str[-5:])
        except:
            strike = 0.0

        return self.log_leg_entry(
            leg_num=leg_num,
            leg_name=leg_name,
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            option_type=option_type,
            strike=strike,
            initial_sl=initial_sl,
            atm_strike=strike  # Use same as strike for now
        )

    def record_leg_exit(self, leg_num: int, exit_price: float, reason: str,
                       realized_pnl: float = None, pnl_percentage: float = None):
        """
        Convenience wrapper for recording leg exit

        Args:
            leg_num: Leg number
            exit_price: Exit price
            reason: Exit reason
            realized_pnl: Realized PnL (optional)
            pnl_percentage: PnL percentage (optional)
        """
        position_id = self.leg_positions.get(leg_num)
        if position_id:
            update_position_status(
                position_id=position_id,
                status=reason,
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                pnl_percentage=pnl_percentage
            )
            metadata_dict = {
                'leg_num': leg_num,
                'exit_price': exit_price,
                'reason': reason
            }
            if realized_pnl is not None:
                metadata_dict['realized_pnl'] = realized_pnl
            if pnl_percentage is not None:
                metadata_dict['pnl_percentage'] = pnl_percentage

            self.log_event(
                "EXIT_EXECUTED",
                f"Leg {leg_num} exited: {reason}",
                metadata=metadata_dict
            )

    def log_sl_update(self, leg_num: int, old_sl: float, new_sl: float):
        """Log stop loss update"""
        self.log_event(
            "SL_UPDATED",
            f"Leg {leg_num} SL updated: {old_sl:.2f} → {new_sl:.2f}",
            metadata={
                'leg_num': leg_num,
                'old_sl': old_sl,
                'new_sl': new_sl
            }
        )
    
    def log_profit_lock_update(self, leg_num: int, old_lock: float, new_lock: float):
        """Log profit lock escalation"""
        self.log_event(
            "PROFIT_LOCK_UPDATED",
            f"Leg {leg_num} profit lock updated: {old_lock:.1f}% → {new_lock:.1f}%",
            metadata={
                'leg_num': leg_num,
                'old_lock_pct': old_lock,
                'new_lock_pct': new_lock
            }
        )
    
    def log_leg_exit(self, leg_num: int, exit_price: float, exit_reason: str, pnl: float, pnl_pct: float):
        """Log leg exit"""
        position_id = self.leg_positions.get(leg_num)
        if position_id:
            update_position_status(
                position_id=position_id,
                status=exit_reason,
                exit_price=exit_price,
                realized_pnl=pnl,
                pnl_percentage=pnl_pct
            )
            
            self.log_event(
                "EXIT_EXECUTED",
                f"Leg {leg_num} exited: {exit_reason} - PnL: ₹{pnl:.2f} ({pnl_pct:.2f}%)",
                metadata={
                    'leg_num': leg_num,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct
                }
            )
    
    def close_session(self, total_pnl: float, total_pnl_pct: float):
        """Close the strategy session"""
        update_session_status(
            session_id=self.session_id,
            status='COMPLETED',
            notes=f"Total PnL: ₹{total_pnl:.2f} ({total_pnl_pct:.2f}%)"
        )
        
        self.log_event(
            "STRATEGY_STOP",
            f"Strategy completed - Total PnL: ₹{total_pnl:.2f} ({total_pnl_pct:.2f}%)",
            metadata={
                'total_pnl': total_pnl,
                'total_pnl_pct': total_pnl_pct
            }
        )
        
        logger.info(f"✓ Session closed - Final PnL: ₹{total_pnl:.2f} ({total_pnl_pct:.2f}%)")
    
    @staticmethod
    def detect_crashed_sessions() -> List[Dict]:
        """
        Detect sessions that were running but didn't complete normally (crashed)
        
        Returns:
            List of crashed session data with active positions
        """
        try:
            # Find sessions with status RUNNING (they should have been marked COMPLETED)
            crashed_sessions = db_session.query(SehwagSession).filter(
                SehwagSession.status == 'RUNNING'
            ).all()
            
            if not crashed_sessions:
                logger.info("✓ No crashed sessions detected")
                return []
            
            logger.warning(f"⚠️  Found {len(crashed_sessions)} crashed session(s)")
            
            result = []
            for session in crashed_sessions:
                # Get active positions from this session
                active_positions = db_session.query(SehwagPosition).filter(
                    SehwagPosition.session_id == session.id,
                    SehwagPosition.status == 'ACTIVE'
                ).all()
                
                if active_positions:
                    result.append({
                        'session_id': session.session_id,
                        'session_db_id': session.id,
                        'start_time': session.start_time,
                        'expiry_date': session.expiry_date,
                        'active_positions': [
                            {
                                'leg_num': pos.leg_num,
                                'leg_name': pos.leg_name,
                                'symbol': pos.symbol,
                                'entry_price': pos.entry_price,
                                'quantity': pos.quantity,
                                'option_type': pos.option_type,
                                'strike': pos.strike,
                                'current_sl': pos.current_sl,
                                'lock_profit_pct': pos.lock_profit_pct,
                                'position_db_id': pos.id
                            }
                            for pos in active_positions
                        ]
                    })
                    logger.warning(f"  Session {session.session_id}: {len(active_positions)} active position(s)")
            
            return result
            
        except Exception as e:
            logger.error(f"Error detecting crashed sessions: {e}")
            return []
    
    @staticmethod
    def mark_sessions_as_crashed(session_ids: List[str], crash_reason: str = "Process terminated unexpectedly"):
        """
        Mark sessions as crashed
        
        Args:
            session_ids: List of session IDs to mark as crashed
            crash_reason: Reason for crash
        """
        try:
            for session_id in session_ids:
                session = db_session.query(SehwagSession).filter(
                    SehwagSession.session_id == session_id
                ).first()
                
                if session:
                    session.status = 'CRASHED'
                    session.end_time = datetime.now()
                    session.notes = (session.notes or '') + f"\n[CRASHED] {crash_reason}"
                    
                    # Log crash event
                    log_event(
                        session_id=session_id,
                        event_type='CRASH_DETECTED',
                        description=f"Session marked as crashed: {crash_reason}"
                    )
            
            db_session.commit()
            logger.info(f"✓ Marked {len(session_ids)} session(s) as CRASHED")
            
        except Exception as e:
            logger.error(f"Error marking sessions as crashed: {e}")
            db_session.rollback()
    
    @staticmethod
    def mark_positions_as_recovered(position_ids: List[int]):
        """
        Mark positions as recovered from crash
        
        Args:
            position_ids: List of position database IDs to mark as recovered
        """
        try:
            for pos_id in position_ids:
                position = db_session.query(SehwagPosition).filter(
                    SehwagPosition.id == pos_id
                ).first()
                
                if position:
                    position.status = 'RECOVERED'
                    position.updated_at = datetime.now()
            
            db_session.commit()
            logger.info(f"✓ Marked {len(position_ids)} position(s) as RECOVERED")
            
        except Exception as e:
            logger.error(f"Error marking positions as recovered: {e}")
            db_session.rollback()
