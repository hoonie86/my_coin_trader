from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
# 하단 키보드
def get_main_keyboard():
    # 하단 고정 메뉴 (ReplyKeyboardMarkup)
    return ReplyKeyboardMarkup([
        ["🤖 자동(신중)", "⚡ 자동(즉시)"], # <--- 버튼 분리
        ["📊 실시간 리포트", "💰 금액설정"], 
        ["🔄 모드 초기화", "⏳ 감시 모드"]
    ], resize_keyboard=True)

# 1. 매수 알람 키보드
def get_buy_inline_kb(symbol, current_amt, is_auto=False):
    # [수정] 사용자님이 원하시는 4종류 버튼 구성 및 금액 실시간 반영
    auto_text = "🚫 자동매수 취소" if is_auto else "⚙️ 자동매수 설정"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🚀 {current_amt:,.0f}원 즉시매수", callback_data=f"buy_now:{symbol}"),
            InlineKeyboardButton("🔥 풀매수", callback_data=f"buy_full:{symbol}")
        ],
        [
            InlineKeyboardButton(auto_text, callback_data=f"toggle_buy_auto:{symbol}"),
            InlineKeyboardButton("👀 감시 유지", callback_data=f"set_buy_watch:{symbol}")
        ]
    ])

# 2. 매도 알람 키보드
def get_sell_inline_kb(symbol, is_auto=False):
    auto_text = "🚫 자동매도 취소" if is_auto else "⚙️ 매도 자동전환"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 즉시 전량매도", callback_data=f"sell_all:{symbol}"),
            InlineKeyboardButton("👀 매도 감시 유지", callback_data=f"set_sell_watch:{symbol}")
        ],
        [
            InlineKeyboardButton(auto_text, callback_data=f"toggle_sell_auto:{symbol}"),
            InlineKeyboardButton("🟠 50% 분할매도", callback_data=f"sell_half:{symbol}")
        ]
    ])

def get_amt_kb(current_amt):
    # [수정] ±5,000원 조정 및 3/5/10만 프리셋
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖ 5,000", callback_data="adj_amt:-5000"),
            InlineKeyboardButton(f"{current_amt:,.0f}원", callback_data="none"),
            InlineKeyboardButton("➕ 5,000", callback_data="adj_amt:5000")
        ],
        [
            InlineKeyboardButton("3만", callback_data="set_amt:30000"),
            InlineKeyboardButton("5만", callback_data="set_amt:50000"),
            InlineKeyboardButton("10만", callback_data="set_amt:100000")
        ]
    ])

# 3. 리포트 전용 키보드
def get_report_inline_kb(is_all_auto=False):
    auto_text = "🚫 전종목 자동매도 취소" if is_all_auto else "⚙️ 전종목 자동매도 설정"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(auto_text, callback_data="toggle_all_sell_auto"),
            InlineKeyboardButton("👀 전종목 감시 유지", callback_data="set_all_sell_watch")
        ],
        [InlineKeyboardButton("🔄 전종목 설정 초기화", callback_data="reset_all_sell_status")]
    ])

# 리포트용 개별 매도 버튼
def get_report_manage_kb(symbol):
    """[신규] 특정 종목 관리용 전용 버튼 (매도 및 상태 변경)"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔴 전량 매도", callback_data=f"sell_all:{symbol}"),
            InlineKeyboardButton("🟠 50% 매도", callback_data=f"sell_half:{symbol}")
        ],
        [
            InlineKeyboardButton("🤖 자동 전환", callback_data=f"toggle_sell_auto:{symbol}"),
            InlineKeyboardButton("👀 감시 전환", callback_data=f"set_sell_watch:{symbol}")
        ],
        [InlineKeyboardButton("⬅️ 뒤로가기 (리포트)", callback_data="request_instant_report")]
    ])
def get_sell_signal_kb(symbol, wait_limit=30):
    """
    [신규] 매도 신호 발생 시 사용자 선택 버튼 (파란색 알림용)
    """
    keyboard = [
        [
            # 이 버튼을 눌러야 handle_interaction의 set_pending_30m이 실행됩니다.
            InlineKeyboardButton(f"🟡 {wait_limit}분 유예", callback_data=f"set_pending_30m:{symbol}"),
            InlineKeyboardButton("🔴 즉시 매도", callback_data=f"sell_now:{symbol}")
        ],
        [
            # 수정 후 모습
            InlineKeyboardButton("🟢 매도 무시(유지)", callback_data=f"set_sell_keep:{symbol}"),
            InlineKeyboardButton("🔇 30분 알람 끄기", callback_data=f"mute_30m:{symbol}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_profit_alert_kb(symbol):
    """수익 알람용 버튼: 추가 매수, 전액 매도, 절반 매도"""
    keyboard = [
        [
            InlineKeyboardButton("🛒 추가 매수", callback_data=f"buy_now:{symbol}"),
        ],
        [
            InlineKeyboardButton("🔴 전액 매도", callback_data=f"sell_all:{symbol}"),
            InlineKeyboardButton("🟠 절반 매도", callback_data=f"sell_half:{symbol}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)