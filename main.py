import asyncio
import pandas as pd
import sys
import json
import os
import shutil
from datetime import datetime, timedelta
import threading
inv_lock = threading.RLock()
buy_queue = asyncio.Queue()
# 1. 필수 폴더 생성
for folder in ['logs', 'trades']:
    if not os.path.exists(folder):
        os.makedirs(folder)
        print(f"📁 폴더 생성 완료: {folder}")
import strategy, config, telegram_ui, analyzer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from config import logger, exchange

class StreamToLogger:
    def __init__(self, log_level):
        self.log_level = log_level
    def write(self, buf):
        content = buf.strip()
        if content: self.log_level(content)
    def flush(self): pass

sys.stdout = StreamToLogger(logger.info)
sys.stderr = StreamToLogger(logger.error)

# [전역 상태 관리] - 기존 로직 100% 유지 + 신규 토글 상태 반영
sell_mute_status = {}  # [기능 19] 'AUTO' | 'WATCH'
buy_individual_status = {}  # 종목별 매수 개별 상태
pending_approvals = {}  # [기능 17] 무응답 자동 대응용
highest_rates = {}  # [기능 16] 수익 상승 보고용
last_report_time = datetime.now() - timedelta(days=1)
sell_cooldown = {}
notified_symbols = {}
pending_approvals = {}
profit_alerts = {}
pending_s_buys = {}
GLOBAL_BUY_MODE = "SHIELD" # 기본값: SHIELD(신중), LIGHTNING(즉시)
# [사후분석] 미지 패턴 기록 종목의 60분 후 수익률 추적용 { symbol: (recorded_at, price_at_record) }
missed_60m_tracker = {}
# [추가] 비상 체제 상태를 저장할 딕셔너리 선언
emergency_mode = strategy.emergency_mode
# [평단가 로컬 관리용]
INV_FILE = "trades/inventory.json"


def load_inventory():
    """저장된 인벤토리 파일을 불러옵니다."""
    with inv_lock:
        if os.path.exists(INV_FILE):
            try:
                with open(INV_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Inventory Load Error: {e}")
                return {}
        return {}

def get_symbol_buy_type(symbol, reason=""):
    """
    reason 문자열에서 TYPE1, 2, 3 존재 여부를 파악하여 타입 번호 리턴.
    찾지 못할 경우 인벤토리를 확인하거나 기본값 1을 리턴.
    """
    # 1. reason 문자열에서 TYPE 확인
    if "TYPE1" in reason:
        return 1
    elif "TYPE2" in reason:
        return 2
    elif "TYPE3" in reason:
        return 3
    elif "TYPE4" in reason:
        return 4
    # 2. reason에 정보가 없을 경우 기존 인벤토리 정보 참조
    inv_data = load_inventory()
    sym_only = symbol.split('/')[0]
    inv_item = inv_data.get(symbol) or inv_data.get(sym_only) or {}
    
    return inv_item.get('buy_type', 1)

def save_inventory(symbol, avg_price, quantity, grade="A", buy_type=1, purchase_time=None):
    """평단가, 수량, 그리고 [진입 등급]을 로컬 파일에 안전하게 저장합니다."""
    with inv_lock:
        try:
            inv = load_inventory()
            if avg_price <= 0:
                logger.error(f"❌ [저장실패] {symbol} 비정상 평단가: {avg_price}")
                return
            if purchase_time is None:
                purchase_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # [수정] buy_time을 기록하여 strategy의 '6봉 유예' 로직과 연동
            # [추가] grade를 기록하여 실시간 리포트에서 진입 당시 등급 확인 가능
            existing_data = inv.get(symbol, {})
            current_max = existing_data.get('max_price', float(avg_price)) # 고점 기록이 없으면 평단가로 초기화
    
            inv[symbol] = {
                **existing_data,                    # 기존의 profit_deadline, max_profit_seen 등 유지
                "avg_price": float(avg_price),      
                "purchase_price": float(avg_price), 
                "total_quantity": float(quantity),  
                "max_price": float(current_max),    # 기존 고점 유지
                "grade": str(grade),                
                "buy_type": buy_type,               
                "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "purchase_time": purchase_time
            }
            with open(INV_FILE, "w") as f:
                json.dump(inv, f, indent=4)
            print(f"💾 [기록완료] {symbol} | 등급: {grade} | 평단: {avg_price:,.0f} | 수량: {quantity}")
        except Exception as e:
            logger.error(f"Inventory Save Error: {e}")


# 프로그램 시작 시 메모리에 로드
manual_inventory = load_inventory()


async def safe_market_buy(symbol, cost, grade="A", buy_type=1):
    """시장가 매수 집행 및 진입 등급(grade) 기록 보강. KRW 초과 오류 방지용 보수적 한도 적용."""
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        free_krw = float(balance['free'].get('KRW', 0))
        # [추가] 최소 잔고 방어선: 6,000원 미만이면 매수 시도 자체를 안 함
        if free_krw < 6000:
            config.logger.warning(f"⚠️ 잔고 부족으로 {symbol} 매수 취소 (가용: {free_krw:,.0f}원)")
            return False, "잔고 부족 (6,000원 미만)"
        # [KRW 초과 방지] 수수료·슬리피지·호가 반올림 대비 95% 한도 (bithumb 주문량 초과 오류 방지)
        safe_cost = min(cost, int(free_krw * 0.95))
        config.logger.info(f"🚨 [ORDER CHECK] {symbol} | 요청금액: {cost:,.0f} | 실제가용잔액: {free_krw:,.0f}")
        if safe_cost < 5500:
            config.logger.warning(f"⚠️ 가용 한도({safe_cost:,.0f}원)가 최소 주문금액(5,000원) 미달로 {symbol} 매수 취소")
            return False, "주문 가능 한액 부족"
        
        import math
        for i in range(3):
            orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
            bid1, ask1 = orderbook['bids'][0][0], orderbook['asks'][0][0]
            gap = ask1 - bid1
            
            # 1차: 매수1호가 알박기 | 2차: 갭 1/4 지점 | 3차: 중간가(마지노선)
            targets = [bid1 + (gap * 0.05), bid1 + (gap * 0.25), bid1 + (gap * 0.5)]
            
            raw_target_p = targets[i]
            tick_size = strategy.get_bithumb_tick_size(raw_target_p)
            
            # 1. 가격: 틱 단위로 무조건 버림(Floor) 처리하여 규격 강제 준수
            target_p = math.floor(raw_target_p / tick_size) * tick_size
            if tick_size >= 1:
                target_p = int(target_p) # 1,000원 이상은 정수형 강제 변환
            else:
                target_p = round(target_p, 4) # 소수점 틱 발생 시 부동소수점 오차 방지
                
            # 2. 수량: 안전 잔고(safe_cost) 기반으로 소수점 4자리 완전 버림 처리
            raw_amount = safe_cost / target_p
            amount = math.floor(raw_amount * 10000) / 10000.0
            
            target_p = float(exchange.price_to_precision(symbol, target_p))
            amount = float(exchange.amount_to_precision(symbol, amount))

            # 3. 디버그 로그: API 서버로 전송되기 직전의 날것(Raw) 데이터 검증
            raw_total = target_p * amount
            logger.info(
                f"📝 [API 전송 검증] {symbol}\n"
                f" - 호가단위(Tick): {tick_size}\n"
                f" - 전송 가격: {target_p} (타입: {type(target_p).__name__})\n"
                f" - 전송 수량: {amount} (타입: {type(amount).__name__})\n"
                f" - 주문 총액: {raw_total:,.2f}원 (가용: {free_krw:,.2f}원)"
            )
                        
            order = await asyncio.to_thread(exchange.create_limit_buy_order, symbol, amount, target_p)
            logger.info(f"⏳ [{i+1}차 낚시] {symbol} | 가격: {target_p:,.2f} | 30초 대기 시작...")
            await asyncio.sleep(30) # 30초 대기
            
            os_status = await asyncio.to_thread(exchange.fetch_order, order['id'], symbol)
            if os_status['status'] == 'closed':
                save_inventory(symbol, target_p, amount, grade, buy_type)
                return True, f"{i+1}차 지정가 체결 완료"
            
            ###### [수정 시작] 취소 시 에러 방지 및 찰나의 체결 확인 로직 (알람 누락 해결) ######
            # 미체결 시 취소 로직 (Race Condition 방지)
            try:
                await asyncio.to_thread(exchange.cancel_order, order['id'], symbol, params={'side': 'buy'})
            except Exception as cancel_e:
                logger.warning(f"⚠️ 주문 취소 실패 (찰나의 순간 체결완료 가능성): {cancel_e}")
            
            # 취소 시도 후 최종 상태 재확인 (여기서 알람 누락이 발생했음)
            try:
                final_status = await asyncio.to_thread(exchange.fetch_order, order['id'], symbol)
                if final_status['filled'] > 0:
                    save_inventory(symbol, target_p, final_status['filled'], grade, buy_type)
                    return True, "부분 체결 혹은 찰나의 전체 체결 완료"
            except:
                # 재조회 실패 시 기존 os_status 의존
                if os_status['filled'] > 0:
                    save_inventory(symbol, target_p, os_status['filled'], grade, buy_type)
                    return True, "부분 체결 완료"
            
            logger.info(f"🚫 {i+1}차 입질 없음, 가격 미세 상향 시도")

        return False, "중간가까지 대기했으나 미체결(포기)"
    except Exception as e:
        logger.error(f"Market Buy Error ({symbol}): {e}")
        return False, str(e)


async def get_my_assets():
    """[수익률 해결] inventory.json(로컬)을 API보다 우선 참조하여 -100% 원천 차단"""
    # 변수 미정의 오류 방지를 위해 최상단 선언
    inv, assets, my_assets = {}, {}, {}
    try:
        # 빗썸 Private API 필수 규격 적용
        params = {'currency': 'ALL', 'payment_currency': 'KRW'}
        balance = await asyncio.to_thread(exchange.fetch_balance, params=params)
        
        if os.path.exists(INV_FILE):
            with open(INV_FILE, "r") as f:
                inv = json.load(f)

        # 보유 원화(KRW) 처리
        krw_total = float(balance['total'].get('KRW', 0))

        is_inv_changed = False # 파일 저장 트리거

        # 빗썸 API의 상세 데이터 추출 (보조용)
        raw_info = balance.get('info', {}).get('data', {})

        exchange_total = balance.get('total', {})
        # [7번] 유령 데이터 청소: 거래소 잔고에 없는 인벤토리 데이터 즉시 삭제
        for inv_sym in list(inv.keys()):
            coin_part = inv_sym.split('/')[0]
            if float(exchange_total.get(coin_part, 0)) <= 0.0001:
                del inv[inv_sym]
                is_inv_changed = True

        for coin, total_val in balance['total'].items():
            total = float(total_val)
            if total <= 0.0001 or coin == 'KRW':
                continue

            symbol = f"{coin}/KRW"
            ###### [수정 시작] 5,000원 미만 소액 자산(Dust) 감시 제외 및 자동 삭제 ######
            # 현재가를 조회하여 실제 평가금액 산출
            try:
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                curr_p = float(ticker.get('last') or 0)
            except Exception as e:
                logger.error(f"Ticker fetch error for {symbol}: {e}")
                curr_p = 0
                
            total_value = total * curr_p
            
            # 평가금액이 5,100원 미만이면 먼지(Dust)로 간주
            if 0 < total_value < 5100:
                logger.info(f"🧹 [소액 자산 필터링] {symbol} 감시 제외 (평가금액: {total_value:,.0f}원)")
                if symbol in inv:
                    del inv[symbol]
                    is_inv_changed = True  # 파일 물리 저장 트리거 작동
                continue  # 아래 평단가 계산 및 my_assets 추가 로직을 완전히 스킵
            # 1단계: 거래소 API 평단가 먼저 시도 (추가매수 반영 및 최신 데이터 우선)
            coin_info = raw_info.get(coin, {})
            try:
                avg_p = float(
                    coin_info.get('avg_buy_price') or
                    coin_info.get('avg_buy_price_all') or
                    coin_info.get('average_price') or
                    0
                )
            except:
                avg_p = 0

            # 2단계: 거래소 데이터가 없거나 0일 때만 로컬 인벤토리(inventory.json) 참조
            local_item = inv.get(symbol) or inv.get(coin) or {}
            if avg_p == 0:
                local_avg = float(local_item.get('purchase_price') or local_item.get('avg_price') or local_item.get('avg_buy_price') or 0)
                
                # [개선] 매수 초기(10분 이내)에만 괴리율 검사 수행
                try:
                    buy_time_str = local_item.get('purchase_time') or local_item.get('buy_time')
                    if buy_time_str:
                        buy_time = datetime.strptime(buy_time_str, '%Y-%m-%d %H:%M:%S')
                        age_minutes = (datetime.now() - buy_time).total_seconds() / 60
                        
                        # # [5번] 10분 초과 시 시간을 현재로 리셋하여 보정 엔진 강제 활성화
                        # if age_minutes > 10:
                        #     local_item['purchase_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        #     age_minutes = 0
                        #     inv[symbol] = local_item
                        #     is_inv_changed = True
                            
                        # 10분 이내인데 1% 이상 괴리가 나면 '기록 오류'로 간주하여 방어
                        if age_minutes < 10:
                            ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                            curr_p = float(ticker.get('last') or 0)
                            if curr_p > 0 and local_avg > 0:
                                diff = abs(curr_p - local_avg) / local_avg
                                # [2번] 손실 상황(현재가 < 평단가)에서만 보정하여 수익 세탁 방지
                                if diff > 0.01 and curr_p < local_avg: 
                                    logger.warning(f"⚠️ {symbol} 진입 초기 괴리 감지 -> 평단가 임시 보정")
                                    local_avg = curr_p
                                    local_item['purchase_price'] = local_avg
                                    inv[symbol] = local_item
                                    is_inv_changed = True
                except:
                    pass
                avg_p = local_avg

            # [교정 3순위] 그래도 0이면 -100% 방지를 위해 '마지막 거래가' 참조
            if avg_p == 0:
                try:
                    # xcoin_last_... 필드나 틱커 데이터 활용
                    avg_p = float(raw_info.get(f'xcoin_last_{coin.lower()}', 0))
                except:
                    avg_p = 0

            assets[symbol] = {
                'avg_price': avg_p,
                'total': total,
                # 수정: purchase_time으로 키 명칭 통일
                'purchase_time': local_item.get('purchase_time') or local_item.get('buy_time') or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'grade': local_item.get('grade', 'B'),    # 등급 추가
                'buy_type': local_item.get('buy_type', 1) # 타입 추가
            }
        if is_inv_changed:
            try:
                with open(INV_FILE, "w") as f:
                    json.dump(inv, f, indent=4)
            except Exception as e:
                logger.error(f"Asset loop Inventory Save Error: {e}")
        return assets
    except Exception as e:
        logger.error(f"❌ Asset Fetch Error 상세 : {e}")
        return {} # 에러 시 빈 딕셔너리 반환하여 루프 중단 방지


async def get_buy_cost():
    """[기능 20] 가용 원화 기반 안전한 투입 금액 산출 (오류 방지용)"""
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        free_krw = float(balance['free'].get('KRW', 0))

        # 사용자 설정 금액 (기본 1만)
        target_cost = config.DEFAULT_TEST_BUY

        # [수정] 수수료 및 호가 변동 대비 여유율을 95%로 상향
        ###### [수정] 잦은 잔고 초과 에러 방지를 위해 95% -> 90%로 조정
        actual_cost = min(target_cost, free_krw * 0.90)

        # 빗썸 최소 주문 금액은 1,000원임
        if actual_cost < 1000:
            return 0

        return int(actual_cost)  # 정수형으로 반환
    except Exception as e:
        logger.error(f"Cost Calculation Error: {e}")
        return 0


async def buy_scan_task(app):
    """매수 스캔 태스크: 들여쓰기 교정 및 S급 추적 로직 정상화 + 1분봉 수급/미지패턴/60분수익률 연동"""
    global notified_symbols, buy_individual_status, pending_s_buys, missed_60m_tracker
    while True:
        try:
            m_data = await asyncio.to_thread(exchange.load_markets, params={'quoteId': 'KRW'})
            markets = list(m_data.values())
            
            assets = await get_my_assets()
            owned_symbols = set(assets.keys())
            
            is_night = config.is_sleeping_time()
            w_list = strategy.get_warning_list()
            
            current_buy_mode = getattr(config, 'buy_mute_mode', 'WATCH')
            current_display_mode = "AUTO (야간)" if is_night else current_buy_mode

            krw_filtered = [
                m for m in markets
                if m['quote'] == 'KRW' and m['active']
                   and m['symbol'].split('/')[0] not in w_list
            ]

            # [수정] dna_collector 성공 로직: 전체 티커를 quoteId 파라미터로 안전하게 호출
            try:
                all_tickers = await asyncio.to_thread(exchange.fetch_tickers, params={'quoteId': 'KRW'})
                # [추가: 정기 점검 등 비정상 응답 방어]
                if not isinstance(all_tickers, dict):
                    logger.warning("⚠️ [입구 컷] fetch_tickers: 딕셔너리가 아닌 응답 (정기점검 의심). 스캔을 1회 건너뜁니다.")
                    await asyncio.sleep(60)
                    continue
            except Exception as e:
                logger.error(f"fetch_tickers 오류: {e}")
                await asyncio.sleep(60)
                continue

            market_rates = [
                float(all_tickers[m['symbol']]['percentage']) for m in krw_filtered 
                if isinstance(all_tickers, dict) and m['symbol'] in all_tickers and all_tickers[m['symbol']].get('percentage') is not None
            ]
            if market_rates:
                current_market_avg = sum(market_rates) / len(market_rates)
                # [수정 시작] strategy에서 리턴하는 상태 변화 여부와 메시지를 변수에 담음
                is_changed, panic_msg = await strategy.update_market_panic_status(current_market_avg)
                if is_changed and panic_msg:
                    await app.bot.send_message(config.CHAT_ID, panic_msg)
                # [수정 끝]
                
                if strategy.is_buy_locked:
                    if not strategy.panic_msg_sent:
                        await app.bot.send_message(config.CHAT_ID, f"🚨 [시장 잠금] Panic Filter 작동 중\n현재 시장 평균: {current_market_avg:+.2f}%\n기준점: {strategy.market_ref_rate:+.2f}%\n매수 스캔을 중단합니다.")
                        strategy.panic_msg_sent = True
                else:
                    # 잠금 상태였다가 해제되는 순간 알림 발송
                    if strategy.panic_msg_sent:
                        await app.bot.send_message(
                            config.CHAT_ID, 
                            f"✅ [시장 해제] 반등 확인으로 매수 스캔을 재개합니다.\n"
                            f"현재 시장 평균: {current_market_avg:+.2f}%"
                        )
                        strategy.panic_msg_sent = False

                # 시장 현황 보고 (사용자 확인용)
                lock_status = "🚨 [LOCK]" if strategy.is_buy_locked else "✅ [NORMAL]"
                print(f"\n📊 [시장 현황] {lock_status} | 현재평균: {current_market_avg:+.2f}% | 기준점: {strategy.market_ref_rate:+.2f}%")
                if strategy.is_buy_locked:
                    print(f"   💡 해제까지: {current_market_avg:+.2f}% -> {strategy.market_ref_rate + 2.0:+.2f}% 필요")
            if strategy.is_buy_locked:
                print(f"🚫 [시장잠금] Panic Filter 상태입니다. 스캔을 생략하고 5분 뒤 시장을 재확인합니다.")
                await asyncio.sleep(300)
                continue
            # [사후분석] 60분 경과한 미지 기록 종목 수익률 로그 업데이트 (기존 로직과 겹치지 않도록 먼저 처리)
            now = datetime.now()
            for sym in list(missed_60m_tracker.keys()):
                rec_at, price_at = missed_60m_tracker[sym]
                if (now - rec_at).total_seconds() >= 3600:
                    try:
                        ticker = await asyncio.to_thread(exchange.fetch_ticker, sym)
                        price_60m = float(ticker.get('last') or ticker.get('close') or 0)
                        if price_60m:
                            analyzer.update_missed_opportunity_return(sym, rec_at.strftime('%Y-%m-%d %H:%M:%S'), price_at, price_60m)
                    except Exception as e:
                        logger.error(f"60m return check error {sym}: {e}")
                    del missed_60m_tracker[sym]

            print(f"\n🔎 [매수 스캔] {len(krw_filtered)}종목 시작 | 모드: {current_display_mode}")

            # 1. 전 종목 스캔 루프
            for idx, m in enumerate(krw_filtered):
                symbol = m['symbol']
                # ========== [쿨다운 체크 로직 추가] ==========
                if symbol in sell_cooldown:
                    if (datetime.now() - sell_cooldown[symbol]).total_seconds() < 3600:
                        continue
                ## [[ MODIFIED: 보유 종목은 분석 및 추천에서 즉시 제외 (서버 부하 감소) ]]
                if symbol in owned_symbols:
                    continue

                # 1. 출력 빈도를 줄여 I/O 부하 감소
                if (idx + 1) % 10 == 0 or idx == 0: 
                    sys.stdout.write(f"\r▶ 스캔 중: [{idx + 1}/{len(krw_filtered)}] {symbol:<12}")
                    sys.stdout.flush()

                await asyncio.sleep(0.5)
                # [예외 처리] 지원하지 않는 마켓(symbollist 미포함) 방어
                markets_dict = getattr(exchange, 'markets', None)
                if markets_dict is not None and symbol not in markets_dict:
                    logger.info(f"지원하지 않는 마켓: {symbol}")
                    continue

                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=500)
                if len(ohlcv) < 281: continue

                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                is_buy, reason, grade, data_dict = await strategy.check_buy_signal(exchange, df, symbol, w_list)
                extracted_type = "1"
                if "TYPE1" in reason: extracted_type = "1"
                elif "TYPE2" in reason: extracted_type = "2"
                elif "TYPE3" in reason: extracted_type = "3"
                elif "TYPE4" in reason: extracted_type = "4"
                # [분석 봇] 매수하지 않더라도 탈락 사유·패턴태그·등급 포함 상세 수치 기록 (조건 1개라도 만족/3분 내 3% 급등 포함)
                current_price = float(df.iloc[-1]['close'])
                if not is_buy and reason:
                    analyzer.record_missed_opportunity(symbol, reason, current_price, data_dict)
                    # [사후분석] 기록된 종목 60분 후 수익률 로그 업데이트용 등록 (조건 만족/3%급등 포함 모든 미지 기록)
                    missed_60m_tracker[symbol] = (datetime.now(), current_price)

                if is_buy:
                    logger.info(f"DEBUG: 💰 {symbol} 매수 시그널 포착 | 등급: {grade} | 이유: {reason}")

                    if symbol in notified_symbols and (datetime.now() - notified_symbols[symbol]) < timedelta(hours=1):
                        continue

                    current_grade = grade if grade else "S"
                    notified_symbols[symbol] = datetime.now()
                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    free_krw = float(balance['free'].get('KRW', 0))
                    buy_cost = await get_buy_cost()
                    # # TYPE 4일 경우에만 예산을 2/4로 축소 (신규 로직 검증용)
                    # if extracted_type == "4":
                    #     buy_cost = int(buy_cost / 2)
                    #     logger.info(f"🛡️ [리스크관리] {symbol} TYPE4 감지로 매수금액 50% 축소: {buy_cost:,.0f}원")
                    buy_type = get_symbol_buy_type(symbol, reason)

                    # [개선] grade 값 우선 사용, 없으면 reason에서 추출
                    is_s_class_check = (grade and grade.startswith("S")) or any(x in reason for x in ["S급", "[S]", "[S+]"])
                    indiv_mode_check = buy_individual_status.get(symbol)
                    curr_mode_check = indiv_mode_check if indiv_mode_check else ("AUTO" if is_night else config.buy_mute_mode)
                    # [개선] 야간이거나 사용자가 즉시 모드(LIGHTNING)를 선택한 경우 즉시 실행
                    is_immediate = is_night or GLOBAL_BUY_MODE == "LIGHTNING"

                    if is_s_class_check and curr_mode_check == "AUTO":
                        if is_immediate:
                            success, msg = await safe_market_buy(symbol, buy_cost, current_grade, extracted_type)
                            if success:
                                # ======== [수정: 매수 완료 알림에 평균매수가 추가] ========
                                temp_inv = load_inventory()
                                actual_price = float(temp_inv.get(symbol, {}).get('purchase_price', 0))
                                buy_msg = (
                                    f"⚡ [즉시 매수 완료] {symbol}\n"
                                    f"(모드: {'야간' if is_night else GLOBAL_BUY_MODE})\n"
                                    f"━━━━━━━━━━━━━━\n"
                                    f"💵 평균 매수가: {actual_price:,.2f}원\n"
                                    f"💰 매수 금액: {buy_cost:,.0f}원\n"
                                    f"💎 사유: {reason}\n"
                                    f"━━━━━━━━━━━━━━"
                                )
                                await app.bot.send_message(config.CHAT_ID, buy_msg)
                            continue
                        
                        if symbol not in pending_s_buys:
                            pending_s_buys[symbol] = {
                                'start_time': datetime.now(),
                                'last_check_min': 0,
                                'reason': reason,
                                'cost': buy_cost
                            }
                            await app.bot.send_message(
                                config.CHAT_ID,
                                f"🔔 [S급 포착] 10분 자동매수 추적 시작\n종목: {symbol}\n사유: {reason}\n\n※ 3분마다 지표 재확인 후 10분 뒤 강제 매수합니다.",
                                reply_markup=telegram_ui.get_buy_inline_kb(symbol, buy_cost, False)
                            )
                        continue
                    # [매수 집행/알림 로직]
                    indiv_mode = buy_individual_status.get(symbol)
                    curr_mode = indiv_mode if indiv_mode else ("AUTO" if is_night else config.buy_mute_mode)
                    #########################################################
                    # [수정] 등급 판정 및 타입별 자동 매수 필터링
                    # S+, S는 'S'로 / A+, A는 'A'로 통합 판정
                    # reason 문자열을 분석하여 실시간 등급(current_grade) 확정
                    # strategy에서 리턴한 grade 값을 우선 사용
                    if grade:
                        current_grade = grade
                    else:
                        # 하위 호환성 유지
                        if any(x in reason for x in ["S급", "[S]"]): current_grade = "S"
                        elif "A" in reason: current_grade = "A"
                        else: current_grade = "B"
                        
                    can_auto_buy = False
                    if curr_mode == "AUTO":
                        if current_market_avg >= 2.0:   # 시장이 2% 상승
                        # if current_grade in ["S", "A"]: can_auto_buy = True # 호황일 때 A급까지
                        # else:
                            if "S" in current_grade: can_auto_buy = True # 일반 시황 S급만

                    if can_auto_buy:
                    #########################################################
                        if free_krw < 1000:
                            await app.bot.send_message(config.CHAT_ID, f"❌ [S급 자동매수 실패] {symbol}\n사유: 잔액 부족")
                        else:
                            # buy_cost와 잔액의 99% 중 작은 값을 선택하여 수수료 에러 방지
                            final_buy_cost = min(buy_cost, free_krw * 0.99)
                            
                            if final_buy_cost < 5000: # 빗썸 최소 주문액 미달 시
                                print(f"⚠️ [잔액부족] {symbol} 매수 스킵 (가용:{free_krw:,.0f}원)")
                                continue

                            ###### [수정 지점 3 교체 시작] 기존 대기 로직 삭제 후 큐(Queue) 예약으로 변경 ######
                            buy_job = {
                                'symbol': symbol, 
                                'cost': final_buy_cost, 
                                'grade': current_grade, 
                                'buy_type': extracted_type,
                                'reason': reason
                            }
                            buy_queue.put_nowait(buy_job)
                            config.logger.info(f"📥 [큐 예약] {symbol} 매수 워커로 전달 (스캔 계속됨)")
                            
                            if symbol in pending_s_buys: 
                                del pending_s_buys[symbol]
                            ###### [수정 지점 3 교체 끝] ######

                    else:
                        display_grade = f"{current_grade}급"
                        # if display_grade == "B급":    continue
                        if ("A급" in display_grade or "B급" in display_grade or "A+급" in display_grade or "B+급" in display_grade):  continue
                        status_tag = f"💎 [매수포착 - {display_grade}]" if not is_s_class_check else "🔥 [S급 포착/수동대기]"
                        is_auto_btn = (indiv_mode == 'AUTO')
                        await app.bot.send_message(
                            config.CHAT_ID,
                            f"{status_tag} {symbol}\n💡 등급: {reason}\n💰 설정금액: {buy_cost:,.0f}원\n💳 가용잔액: {free_krw:,.0f}원",
                            reply_markup=telegram_ui.get_buy_inline_kb(symbol, buy_cost, is_auto_btn)
                        )

            print(f"\n✅ 스캔 완료 | {datetime.now().strftime('%H:%M:%S')}")
            await asyncio.sleep(300)

        except Exception as e:
            logger.error(f"Buy Task Error: {e}")
            await asyncio.sleep(60)

def get_next_tick_down(price):
    if price < 10: return round(price - 0.01, 2)
    if price < 100: return round(price - 0.1, 1)
    if price < 1000: return int(price - 1)
    if price < 5000: return int(price - 5)
    if price < 10000: return int(price - 10)
    if price < 50000: return int(price - 50)
    if price < 100000: return int(price - 100)
    if price < 500000: return int(price - 500)
    return int(price - 1000)

async def execute_sell(app, symbol, reason):
    """
    실제 거래소 매도 주문을 실행하고, 인벤토리 정리 및 거래 로그를 기록한 뒤 알림을 보냅니다.
    """
    try:
        # 1. 현재 잔고 확인
        balance = await asyncio.to_thread(exchange.fetch_balance)
        base = symbol.split('/')[0]
        quantity = float(balance['total'].get(base, 0))

        ###### 거래소 수량 정밀도 맞춤 (먼지 잔고 방지) ######
        try:
            precision_qty = float(exchange.amount_to_precision(symbol, quantity))
        except:
            precision_qty = quantity
            
        is_urgent = ("[긴급]" in reason or "🚨" in reason)
        if is_urgent:
            # 2-A. 긴급 상황 시 시장가 즉시 매도 (기존 로직)
            order_result = await asyncio.to_thread(exchange.create_market_sell_order, symbol, precision_qty)
            logger.info(f"DEBUG: 💰 {symbol} 긴급 매도(시장가) 완료: {reason} | 수량: {precision_qty}")
        else:
            # 2-B. 일반 수익 보전 시 3단계 지정가 매도
            wait_sec = 10  # 대기 시간 10초
            fill_success = False
            
            for i in range(3):
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                current_p = float(ticker['last'])
                
                target_p = current_p
                for _ in range(i):
                    target_p = get_next_tick_down(target_p)
                    
                logger.info(f"⏳ [지정가매도 {i+1}차] {symbol} | 목표가: {target_p} | {wait_sec}초 대기")
                order = await asyncio.to_thread(exchange.create_limit_sell_order, symbol, precision_qty, target_p)
                
                await asyncio.sleep(wait_sec)
                
                check = await asyncio.to_thread(exchange.fetch_order, order['id'], symbol)
                if check['status'] == 'closed':
                    fill_success = True
                    order_result = check
                    logger.info(f"DEBUG: 💰 {symbol} 지정가 매도 체결: {reason} | 수량: {precision_qty} | 체결가: {target_p}")
                    break
                else:
                    await asyncio.to_thread(exchange.cancel_order, order['id'], symbol)
                    logger.info(f"⏭️ [미체결취소] {symbol} {i+1}차 지정가 실패. 주문을 취소합니다.")
            
            # 3회 모두 실패 시 유예 재시작 및 함수 종료
            if not fill_success:
                alert_msg = f"❌ [매도보류] {symbol} 호가창 얇음 (슬리피지 방어)\n사유: {reason}\n지정가 매도 3회 실패로 3분 유예를 재시작합니다."
                await app.bot.send_message(config.CHAT_ID, alert_msg)
                logger.warning(alert_msg)
                
                # 인벤토리 데이터 불러와서 deadline 3분 연장
                try:
                    inv_data = load_inventory()
                    if symbol in inv_data:
                        inv_data[symbol]['profit_deadline'] = (datetime.now() + timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M:%S')
                        with open(INV_FILE, "w") as f:
                            json.dump(inv_data, f, indent=4)
                except Exception as e:
                    logger.error(f"Failed to reset deadline: {e}")
                
                return  # 즉시 함수 종료 (인벤토리 삭제 방지)
        # 3. 비상 모드 판정 및 6시간 쿨다운 설정
        lvl = emergency_mode.get(symbol, 0)
        if lvl >= 1:
            strategy.cooldown_dict[symbol] = datetime.now() + timedelta(hours=6)
            alert_header = f"🚨 [비상 엔진 L{lvl} 익절]"
            emergency_mode.pop(symbol, None) # 비상 모드 즉시 해제
            logger.info(f"✨ {symbol} 비상 체제(L{lvl}) 종료 및 6시간 쿨다운 적용")
        else:
            alert_header = "💰 [매도 완료]"

        # 4. 수익률 계산을 위한 평단가 확보
        inv = load_inventory()
        item = inv.get(symbol, {})
        avg_buy_price = float(item.get('avg_price') or item.get('purchase_price') or 0)
        
        if avg_buy_price == 0:
            current_assets = await get_my_assets()
            avg_buy_price = current_assets.get(symbol, {}).get('avg_price', 0)

        # 5. 실제 체결가(sell_price) 산출 로직
        orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
        curr_bid = float(orderbook['bids'][0][0]) # 시장가 매도는 매수 1호가(bid)에 체결됨
        
        sell_price = float(order_result.get('average') or order_result.get('price') or 0)
        
        # 빗썸 체결가 지연 대응 (fetch_order 재조회)
        if sell_price == 0 and order_result.get('id'):
            try:
                await asyncio.sleep(0.5) 
                updated_order = await asyncio.to_thread(exchange.fetch_order, order_result['id'], symbol)
                sell_price = float(updated_order.get('average') or updated_order.get('price') or curr_bid)
            except:
                sell_price = curr_bid
        
        if sell_price == 0: sell_price = curr_bid
        
        # 6. 최종 수익률 및 로그 기록
        this_profit = ((sell_price - avg_buy_price) / avg_buy_price * 100) if avg_buy_price > 0 else 0
        
        # [누락방지 1] 거래 내역 CSV 저장 (이미 main.py에 있는 save_trade_log 호출)
        save_trade_log(symbol, item.get('grade', 'A'), avg_buy_price, sell_price, this_profit, reason)

        # [누락방지 2] 로컬 인벤토리 파일에서 해당 종목 삭제 (중요)
        if symbol in inv:
            global sell_cooldown
            sell_cooldown[symbol] = datetime.now()
            del inv[symbol]
            with open("trades/inventory.json", "w") as f:
                json.dump(inv, f, indent=4)
            logger.info(f"💾 [인벤토리 정리] {symbol} 데이터 삭제 완료")

        # 7. 텔레그램 최종 알림
        completion_msg = (
            f"✅ [{symbol}] 매도 실행 완료\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 최종 수익률: {this_profit:+.2f}%\n"
            f"💵 체결 단가: {sell_price:,.0f}원\n"
            f"📝 매도 사유: {reason}\n"
            f"━━━━━━━━━━━━━━"
        )
        await app.bot.send_message(config.CHAT_ID, completion_msg)
        
        # 8. 유예 목록 정리
        if symbol in pending_approvals:
            del pending_approvals[symbol]
            
    except Exception as e:
        logger.error(f"❌ {symbol} 매도 집행 중 치명적 에러: {e}")

async def monitor_sell_loop(exchange):
    """들고 있는 종목들을 매수 루프와 상관없이 실시간으로 감시해서 팝니다."""
    logger.info("📡 [실시간 매도 감시 루프 가동]")
    while True:
        try:
            # 1. 현재 잔고 확인 (들고 있는 종목만 추출)
            balances = await asyncio.to_thread(exchange.fetch_balance)
            positions = [b for b in balances['total'] if balances['total'][b] > 0] # 실제 포지션 로직에 맞게 수정
            
            for symbol in positions:
                inv_data = load_inventory().get(symbol)
                if not inv_data: continue

                p_time = datetime.strptime(inv_data['purchase_time'], '%Y-%m-%d %H:%M:%S')
                is_emergency = "S" in inv_data.get('grade', '') or "TYPE3" in inv_data.get('grade', '')
                if (datetime.now() - p_time).total_seconds() < 10800 and not is_emergency: continue

                tf = '3m' if is_emergency else '30m'
                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, tf, limit=50)
                pass
                
            await asyncio.sleep(20) # 20초마다 매도 신호 감시
        except Exception as e:
            logger.error(f"매도 감시 루프 에러: {e}")
            await asyncio.sleep(1)

async def emergency_monitor_task(app):
    """Level 1, 2 종목만 1~2초 주기로 감시하여 지연 없이 즉시 매도 집행"""
    logger.info("📡 [초고속 비상 매도 루프 가동]")
    while True:
        try:
            # 비상 레벨이 1(Caution) 또는 2(Emergency)인 종목만 필터링
            targets = [s for s, l in strategy.emergency_mode.items() if l >= 1]
            
            if not targets:
                await asyncio.sleep(2)
                continue

            assets = await get_my_assets()
            for symbol in targets:
                if symbol not in assets:
                    strategy.emergency_mode.pop(symbol, None) # 팔렸으면 목록 제거
                    continue

                ###### [완성] 비상 종목용 실시간 데이터 수집 및 매도 엔진 호출 ######
                inv_item = load_inventory().get(symbol, {})
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                curr_p = float(ticker.get('last') or 0)
                
                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=200)
                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                
                # 비상 루프는 유예(age=99) 없이 즉시 판단
                is_sell, reason, is_urgent = await strategy.check_sell_signal(
                    exchange, df, symbol, inv_item.get('avg_price', 0), 
                    inv_item.get('max_price', curr_p), 'S', 99, 'AUTO', curr_p, inv_item.get('buy_type', 1)
                )

                if is_sell:
                    await execute_sell(app, symbol, reason)
                
            await asyncio.sleep(1) 
        except Exception as e:
            logger.error(f"Emergency Loop Error: {e}")
            await asyncio.sleep(1)

async def sell_monitor_task(app):
    """[최종 복구] 기존 유예/취소/0순위 로직 완전 유지 + 수익률 & 야간 모드 보정"""
    global last_report_time, sell_mute_status, pending_approvals, profit_alerts, emergency_mode
    while True:
        try:
            # 기본 대기 시간 1분
            current_loop_wait_time = 20
            current_loop_sleep = current_loop_wait_time
            # [추가] 서버 실시간 확인용 시간
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            assets = await get_my_assets()
            # [추가] 등급 및 시간 정보를 정확히 가져오기 위해 인벤토리 로드
            inv_data = load_inventory()

            is_night = config.is_sleeping_time()
            report_lines = []
            symbol_buttons = []

            for symbol, data in list(assets.items()):
                # 0단계: 기본 데이터 수집
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)

                if this_curr_p <= 0:
                    logger.warning(f"⚠️ {symbol} 현재가 조회 실패. 리포트 및 감시를 1회 스킵합니다.")
                    continue

                # 인벤토리 데이터 미리 로드 (평단가 보충 및 등급 확인용)
                # 인벤토리 데이터 미리 로드
                inv_item = inv_data.get(symbol) or inv_data.get(symbol.split('/')[0]) or {}
                
                # [데이터 출처 추적]
                p_inv = inv_item.get('price') or inv_item.get('purchase_price') or inv_item.get('avg_price')
                p_exch = data.get('avg_price')
                buy_type=inv_item.get('buy_type', 1)
                # 1순위: 우리 인벤토리 기록, 2순위: 거래소 데이터
                this_avg_p = float(p_inv or p_exch or 0)
                this_qty = float(data.get('total') or inv_item.get('total_quantity') or 0)
                this_grade = inv_item.get('grade')
                this_purchase_time=inv_item.get('purchase_time')
                
                p_inv_val = float(p_inv or 0)
                p_exch_val = float(p_exch or 0)
                curr_p_val = float(this_curr_p or 0)

                # [분석용 로그] 사자마자 팔리는 원인을 잡기 위해 무조건 출력
                print(f"🔍 [MONITOR] {symbol} | 현재가: {curr_p_val:,.0f} | 평단가: {this_avg_p:,.0f} (기록:{p_inv_val:.2f} / 거래소:{p_exch_val:.2f})")

                if this_avg_p <= 0:
                    # ======== [수정: 강제 0% 보정 제거 및 감시 스킵] ========
                    logger.warning(f"⚠️ [WARN] {symbol} 평단가 조회 실패 -> 0% 수익률 오류 방지를 위해 감시 스킵")
                    continue

                # 수익률 계산 (보정된 평단가 사용)
                this_profit = ((this_curr_p - this_avg_p) / this_avg_p * 100) if this_avg_p > 0 else 0
                ######### [수정: 루프 주기 단축 결정] #########
                # 종목들을 훑다가 한 놈이라도 급등(10%↑) 중이면 
                # 이 루프가 끝난 뒤 잠드는 시간을 30초로 줄입니다.
                # 인벤토리에서 기존 고점 가져오기 (없으면 현재가로 시작)
                current_max_p = float(inv_item.get('max_price', this_curr_p))
                
                # 현재가가 기존 고점보다 높으면 실시간 갱신 (4.2% 등 수익 보전용)
                if this_curr_p > current_max_p:
                    inv_item['max_price'] = this_curr_p
                    current_max_p = this_curr_p
                    # 인벤토리 파일에 실시간 고점 즉시 반영
                    inv_data[symbol] = inv_item
                    try:
                        with open(INV_FILE, "w") as f:
                            json.dump(inv_data, f, indent=4)
                    except Exception as e:
                        logger.error(f"Max Price Save Error: {e}")
                if this_profit >= 10.0 or emergency_mode.get(symbol, False):
                    current_loop_sleep = 10      # 긴급 모드는 5초마다 조회

                this_profit_krw = (this_curr_p - this_avg_p) * this_qty

                # [수정] 인벤토리에서 등급 가져오기
                this_grade = inv_item.get('grade', 'A')

                # 실시간 경과 시간 및 타입 추출
                this_elapsed_bars = 0
                buy_time_str = inv_item.get('purchase_time') or inv_item.get('buy_time') or inv_item.get('last_update') 
                if buy_time_str:
                    try:
                        buy_time_dt = datetime.strptime(buy_time_str, '%Y-%m-%d %H:%M:%S')
                        diff_sec = (datetime.now() - buy_time_dt).total_seconds()
                        this_elapsed_bars = int(diff_sec / 1800)  # 30분봉 기준
                    except:
                        this_elapsed_bars = 0 # 에러 시 0으로 초기화하여 유예 적용
                else:
                    this_elapsed_bars = 99
                ###### [출력] 시간 계산 디버깅 ######
                print(f"DEBUG: {symbol} | buy_time: {buy_time_str} | calc_age: {this_elapsed_bars}")
                # 인벤토리에서 매수 당시 결정된 타입(1, 2, 3)을 가져옵니다.
                this_buy_type = inv_item.get('buy_type', 1)

                # 1단계: 수익 알람 (기존 로직 유지)
                if this_profit >= 1.0:
                    last_alert_p = profit_alerts.get(symbol, 0)
                    if this_profit >= last_alert_p + 1.0:
                        profit_alerts[symbol] = int(this_profit)
                        kb = telegram_ui.get_profit_alert_kb(symbol)
                        await app.bot.send_message(
                            config.CHAT_ID,
                            f"💰 [수익 알람] {symbol.split('/')[0]}\n"
                            f"수익률: {this_profit:+.2f}% ({this_profit_krw:+,.0f}원)\n"
                            f"현재가: {this_curr_p:,.0f}원",
                            reply_markup=kb
                        )

                # 2단계: 차트 데이터 및 익절 엔진 (기존 로직 보존)
                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=100)
                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                ma40_line = df['close'].rolling(40).mean().iloc[-1]

                tp_executed = False
                # # [기존 익절 로직 보존]
                # if this_profit >= 13.0:
                #     balance = await asyncio.to_thread(exchange.fetch_balance)
                #     base = symbol.split('/')[0]
                #     free_qty = float(balance['free'].get(base, 0))
                #     sell_qty = min(this_qty, free_qty)
                #     if sell_qty <= 0:
                #         logger.info(f"매도 건너뜀(잔고 부족): {symbol}")
                #     else:
                #         await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                #         await app.bot.send_message(config.CHAT_ID, f"🎯 [목표익절] {symbol} 13% 전량 매도")
                #         tp_executed = True
                # elif this_profit >= 8.0 and this_curr_p < ma40_line:
                if this_profit >= 8.0 and this_curr_p < ma40_line:    
                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    base = symbol.split('/')[0]
                    free_qty = float(balance['free'].get(base, 0))
                    sell_qty = min(this_qty, free_qty)
                    if sell_qty <= 0:
                        logger.info(f"매도 건너뜀(잔고 부족): {symbol}")
                    else:
                        await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                        await app.bot.send_message(config.CHAT_ID, f"💰 [추적익절] {symbol} 8%구간 40선 이탈")
                        tp_executed = True

                if tp_executed:
                    if symbol in pending_approvals: del pending_approvals[symbol]
                    continue

                # 3단계: 매도 엔진 & 유예 관리 (야간 AUTO 반영)
                m_status = sell_mute_status.get(symbol, 'WATCH')
                # 밤이면 무조건 AUTO로 동작하게 함
                status = 'AUTO' if is_night else m_status

                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)
                realtime_price = this_curr_p  # 실시간 현재가 긁기
                
                # [수정] urgent_flag를 함께 받을 수 있도록 호출부 수정
                res = await strategy.check_sell_signal(
                    exchange=exchange,
                    df=df,
                    symbol=symbol,
                    purchase_price=this_avg_p,
                    max_price=current_max_p,
                    grade=this_grade,
                    symbol_inventory_age=this_elapsed_bars,
                    status=status, 
                    realtime_p=realtime_price, 
                    buy_type=buy_type
                )
                
                # 리턴값 분해 (is_sell, reason, [urgent])
                is_sell_signal = res[0]
                sell_reason = res[1]
                is_urgent = res[2] if len(res) > 2 else False

                ############################################################################
                ######### [신규: 급등주 즉시 매도 비상구] #########
                ############################################################################
                if is_sell_signal and is_urgent:
                    logger.info(f"🚨🚨🚨 {symbol} 긴급 매도 신호 감지! 유예 없이 즉시 집행: {sell_reason}")
                    await execute_sell(app, symbol, f"[긴급]{sell_reason}")
                    continue # 다음 종목으로 점프

                # [추가 로직: 3번 타입 하락 후 상승 종목 전용 방어막] #####
                if is_sell_signal and this_buy_type == 3:
                    # [1순위] 절대 손절선 감시 (6봉 여부와 상관없이 항상 작동)
                    if this_profit <= -3.0:
                        is_sell_signal = True
                        sell_reason = "📉 [3번-절대손절] 매수가 대비 -3% 도달"
                    
                    # [2순위] 유예 기간 및 40선 감시
                    else:
                        # A. 40, 90선 관련 신호는 3번 타입에선 항상 무시 + 6봉(3시간) 이전 무시
                        if "40선" in sell_reason or "90선" in sell_reason or this_elapsed_bars < 6:
                            is_sell_signal = False
                            sell_reason = ""
                        # C. 6봉 이후일 때
                        else:
                            # 40선 이탈 신호가 오면 그대로 수용 (is_sell_signal 유지)
                            if is_sell_signal and "40선" in sell_reason:
                                sell_reason = "⚠️ [3번-유예종료] 6봉 경과 후 40선 이탈"

                # 0순위 급등/절대익절 판정
                if status == 'KEEP' and is_sell_signal and "0순위" in sell_reason:
                    is_sell_final = True
                else:
                    is_sell_final = False
                ###### 수익 10분 유예 타이머 (수익 릴레이) ######
                now_time = datetime.now()
                is_relay_updated = False 
                
                w_list = strategy.get_warning_list()
                is_buy_now, buy_reason_now, _, _ = await strategy.check_buy_signal(exchange, df, symbol, w_list)
                is_t2_signal_alive = (is_buy_now and "TYPE2" in buy_reason_now)
                
                # 1. 수익 릴레이 (매도 유예 중이면 간섭 완전 차단)
                if symbol not in pending_approvals:
                    if this_profit >= 1.0 and not is_urgent and not is_sell_signal:
                        p_deadline = inv_item.get('profit_deadline')
                        if not p_deadline:
                            inv_item['profit_deadline'] = (now_time + timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M:%S')
                            inv_item['max_profit_seen'] = this_profit
                            inv_data[symbol] = inv_item
                            is_relay_updated = True
                            await app.bot.send_message(config.CHAT_ID, f"🚨 [{symbol}] 수익 1% 돌파! 3분 매도 유예 시작")
                        elif this_profit > inv_item.get('max_profit_seen', 0) or is_t2_signal_alive:
                            update_reason = "고점 갱신" if this_profit > inv_item.get('max_profit_seen', 0) else "TYPE2 시그널 유지"
                            inv_item['profit_deadline'] = (now_time + timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M:%S')
                            inv_item['max_profit_seen'] = max(this_profit, inv_item.get('max_profit_seen', 0))
                            inv_data[symbol] = inv_item
                            is_relay_updated = True
                            await app.bot.send_message(config.CHAT_ID, f"🔄 [{symbol}] {update_reason}({this_profit:+.2f}%)! 3분 재설정")

                # 수익 릴레이 만료 시 
                p_deadline = inv_item.get('profit_deadline')
                if p_deadline and now_time > datetime.strptime(p_deadline, '%Y-%m-%d %H:%M:%S'):
                    is_sell_signal = True
                    is_sell_final = True
                    sell_reason = "⏰ [익절] 수익 경신 멈춤으로 3분 유예 만료"
                    del inv_item['profit_deadline']
                    inv_data[symbol] = inv_item
                    is_relay_updated = True

                if is_relay_updated:
                    try:
                        with inv_lock:
                            with open(INV_FILE, "w") as f:
                                json.dump(inv_data, f, indent=4)
                    except Exception as e:
                        logger.error(f"Profit Timer Save Error: {e}")

                # 2. 매도 유예 관리: [2대 원칙 적용]
                if is_sell_signal:
                    if is_urgent:
                        is_sell_final = True
                    elif symbol not in pending_approvals:
                        # [원칙 1] 이미 명단에 있으면 Pass, 없을 때만 5분 번호표 신규 발급
                        wait_limit = 5 
                        pending_approvals[symbol] = {
                            'status': 'NOTIFIED',
                            'start_time': datetime.now(),
                            'entry_profit': this_profit,
                            'reason': sell_reason,
                            'wait_limit': wait_limit
                        }
                        kb = telegram_ui.get_sell_signal_kb(symbol, wait_limit)
                        await app.bot.send_message(
                            config.CHAT_ID,
                            f"🟡 [{wait_limit}분 유예 시작] {symbol}\n사유: {sell_reason}\n"
                            f"현재수익률: {this_profit:+.2f}% | 5분 뒤 자동 매도 판단", reply_markup=kb
                        )

                # 매도 신호 유무 무관, 유예 중인 놈은 상시 감시
                if symbol in pending_approvals:
                    wait_data = pending_approvals[symbol]
                    elapsed_min = (datetime.now() - wait_data['start_time']).total_seconds() / 60
                    current_limit = wait_data.get('wait_limit', 10)
                    
                    if elapsed_min >= current_limit:
                        # [원칙 2] 10분 정각 만료 시점에만 성적 검사
                        if this_profit > wait_data.get('entry_profit', 0):
                            # 개선 시: 보류하고 10분 추가 연장
                            wait_data['start_time'] = datetime.now()
                            wait_data['entry_profit'] = this_profit
                            pending_approvals[symbol] = wait_data
                            await app.bot.send_message(config.CHAT_ID, f"✅ [유예 연장] {symbol} 반등 성공({this_profit:+.2f}%)으로 10분 추가")
                            is_sell_final = False
                        else:
                            # 하락 또는 동일 시: 봐주지 않고 즉시 매도
                            is_sell_final = True
                            sell_reason = f"🚨 [유예만료] 반등 실패 및 매도 집행 ({this_profit:+.2f}%)"

                # 3. 리포트 라인 및 모드 세분화 아이콘 생성
                is_emergency = emergency_mode.get(symbol, 0) >= 1
                is_sell_pending = symbol in pending_approvals
                is_profit_timer = inv_item.get('profit_deadline') is not None

                if status == 'KEEP' and not (is_sell_signal and "0순위" in sell_reason):
                    report_color, status_text, mode_str = "🟢", "유지 중", " 🔒"
                else:
                    report_color, status_text = strategy.get_report_visuals(
                        this_profit, is_sell_signal, this_curr_p, ma40_line,
                        sell_reason, symbol, pending_approvals
                    )
                    mode_icon = " 🤖" if status == 'AUTO' else ""

                # 직관적인 아이콘 우선순위 (초정밀 > 하락유예 > 수익유예 > 일반)
                if is_emergency:
                    mode_str, report_color = "긴급", "🚨"
                elif is_sell_pending:
                    mode_str, report_color = "유예", "🟡"
                elif is_profit_timer:
                    mode_str, report_color = "수익", "🔵"
                else:
                    mode_str = "일반"

                report_line = f"{report_color} [{this_grade} | {mode_str}] {symbol.split('/')[0]:<6} | {this_curr_p:,.0f}원 | {this_profit:+.2f}%({this_profit_krw:+,.0f}원) | {status_text}{mode_icon}"
                report_lines.append({
                    'text': report_line,
                    'profit': this_profit,
                    'button': InlineKeyboardButton(f"🔍 {symbol.split('/')[0]}", callback_data=f"manage_asset:{symbol}")
                })

                # 4. 최종 매도 집행 (불필요한 중복 알림 코드 제거 완료)
                if is_sell_final:
                    await execute_sell(app, symbol, sell_reason)
                    if symbol in pending_approvals: del pending_approvals[symbol]
                    continue
                elif not is_sell_signal:
                    pass # 신호가 해제되어도 사용자의 [원칙 2]에 따라 만료 시점까지 대기함
            # 정기 리포트 발송 (기존 로직 유지)
            if (datetime.now() - last_report_time).total_seconds() >= config.REPORT_INTERVAL:
                if report_lines:
                    ##### [수정/추가] 1. 상세 목록 수익률 내림차순 정렬 #####
                    report_lines.sort(key=lambda x: x['profit'], reverse=True)
                    
                    # 텍스트와 버튼 리스트 재구성
                    final_text_lines = [item['text'] for item in report_lines]
                    sorted_buttons = [item['button'] for item in report_lines]

                    ##### [수정/추가] 2. 요약란 집계 순서 변경: 초 > 파 > 노 > 빨 #####
                    summary = (
                        f"🟢:{sum(1 for l in final_text_lines if '🟢' in l)} | "
                        f"🔵:{sum(1 for l in final_text_lines if '🔵' in l)} | "
                        f"🟡:{sum(1 for l in final_text_lines if '🟡' in l)} | "
                        f"🔴:{sum(1 for l in final_text_lines if '🔴' in l)}"
                    )
                    # 🚨 아이콘이 있는 경우에만 우측에 별도 추가 표기
                    urgent_count = sum(1 for l in final_text_lines if '🚨' in l)
                    if urgent_count > 0:
                        summary += f" | 🚨:{urgent_count}"
                    # 1. 수동 AUTO 상태 판정 (config 참조)
                    is_manual_auto = (getattr(config, 'buy_mute_mode', 'MANUAL') == 'AUTO')
                    
                    # 2. 태그 결정
                    mode_tag = ""
                    if is_night:
                        mode_tag = " (야간 AUTO)"
                    elif is_manual_auto:
                        mode_tag = " (AUTO)"

                    # 3. 메시지 조립
                    msg_text = (
                        f"📊 [정기 리포트] ({now_str}){mode_tag}\n"
                        f"{summary}\n"
                        f"━━━━━━━━━━━━\n"
                        + "\n".join(final_text_lines)
                    )
                    final_rows = [sorted_buttons[i:i + 4] for i in range(0, len(sorted_buttons), 4)]
                    is_all_auto = all(sell_mute_status.get(s) == 'AUTO' for s in assets.keys()) if assets else False
                    report_kb = telegram_ui.get_report_inline_kb(is_all_auto)
                    if report_kb and hasattr(report_kb, 'inline_keyboard'):
                        final_rows.extend(report_kb.inline_keyboard)

                    await app.bot.send_message(config.CHAT_ID, msg_text, reply_markup=InlineKeyboardMarkup(final_rows))
                last_report_time = datetime.now()

            await asyncio.sleep(current_loop_sleep)  # [변경] 매도 감시 주기 current_loop_sleep
        except Exception as e:
            import traceback
            logger.error(f"Sell Monitor Error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(60)  # [변경] 에러 발생 시에도 1분 대기

# main.py 상단 적당한 위치
def save_trade_log(symbol, grade, buy_p, sell_p, profit, reason):
    """매도 결과를 CSV 파일에 기록하여 사후 분석 및 통계용으로 사용"""
    log_file = "trades/trade_history.csv"
    try:
        log_data = {
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'grade': grade,
            'buy_price': buy_p,
            'sell_price': sell_p,
            'profit_pct': round(profit, 2),
            'reason': reason
        }
        df_log = pd.DataFrame([log_data])
        if not os.path.exists(log_file):
            df_log.to_csv(log_file, index=False, encoding='utf-8-sig')
        else:
            df_log.to_csv(log_file, mode='a', header=False, index=False, encoding='utf-8-sig')
    except Exception as e:
        logger.error(f"로그 기록 실패: {e}")

async def handle_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 상호작용 (최종 반영: S급 자동매수 추적 해제 로직 추가)"""
    global sell_mute_status, buy_individual_status, pending_s_buys, GLOBAL_BUY_MODE
    msg = update.message.text if update.message else ""

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        data = query.data.split(':')
        action = data[0]
        symbol = data[1] if len(data) > 1 else None

        # [토글 로직] - 사용자님 기존 로직 그대로 유지
        if action == "toggle_buy_auto":
            current = buy_individual_status.get(symbol)
            new_mode = 'AUTO' if current != 'AUTO' else None
            buy_individual_status[symbol] = new_mode
            await query.edit_message_reply_markup(
                telegram_ui.get_buy_inline_kb(symbol, config.DEFAULT_TEST_BUY, new_mode == 'AUTO'))

        elif action == "toggle_sell_auto":
            current = sell_mute_status.get(symbol)
            new_mode = 'AUTO' if current != 'AUTO' else 'WATCH'
            sell_mute_status[symbol] = new_mode
            await query.edit_message_reply_markup(telegram_ui.get_sell_inline_kb(symbol, new_mode == 'AUTO'))

        elif action == "set_buy_watch":
            buy_individual_status[symbol] = 'WATCH'
            # [추가] 사용자가 감시 유지를 선택하면 S급 자동매수 추적 리스트에서 제거
            if symbol in pending_s_buys: del pending_s_buys[symbol]
            await query.edit_message_text(f"👀 {symbol}\n매수 모드: [감시 유지] 상태입니다.\n(자동 매수 예약이 취소되었습니다.)")

        elif action == "set_sell_watch":
            sell_mute_status[symbol] = 'WATCH'
            if symbol in pending_approvals: del pending_approvals[symbol]
            await query.edit_message_text(f"🔍 {symbol}\n매도 모드: [감시 유지] 상태입니다.")

        elif action == "set_sell_keep":
            sell_mute_status[symbol] = 'KEEP'
            if symbol in pending_approvals: del pending_approvals[symbol]
            await query.edit_message_text(f"🟢 {symbol}\n매도 모드: [매도 무시/유지 🔒] 상태입니다.")

        elif action in ["buy_now", "buy_full"]:
            await query.answer()
            try:
                # [추가] 수동 매수 집행 시 S급 자동매수 추적 리스트에서 즉시 제거
                if symbol in pending_s_buys: del pending_s_buys[symbol]

                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=200)
                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])

                # 기존 get_current_grade 호출 및 매수 로직 유지
                #from main import get_current_grade  # 참조 확인
                current_grade = await get_current_grade(exchange, symbol, df)
                cost = config.DEFAULT_TEST_BUY if action == "buy_now" else 1000000
                reason = query.message.text if query.message and query.message.text else ""
                buy_type = get_symbol_buy_type(symbol, reason)
                logger.info(f"📍 [수동매수 시작] {symbol} | 등급: {current_grade} | 금액: {cost} | 타입: {buy_type}")
                # 변수에 담긴 현재 등급을 전달
                success, res_msg = await safe_market_buy(symbol, cost, current_grade, buy_type)

                if success:
                    display_msg = f"🚀 [{symbol.split('/')[0]}] 매수 성공! | 타입: {buy_type} | 금액: {cost:,}원)"
                else:
                    display_msg = f"❌ [{symbol.split('/')[0]}] 매수 실패\n사유: {res_msg}"
                await query.edit_message_text(display_msg)
            except Exception as e:
                logger.error(f"❌ 매수 프로세스 치명적 오류: {e}")
                await query.edit_message_text(f"⚠️ 시스템 오류로 매수 실패: {e}")

        elif action == "sell_all":
            assets = await get_my_assets()
            if symbol in assets:
                await asyncio.to_thread(exchange.create_market_sell_order, symbol, assets[symbol]['total'])
                if symbol in pending_approvals: del pending_approvals[symbol]
                await query.edit_message_text(f"✅ {symbol} 전량 매도 완료.")

        elif action == "sell_half":
            assets = await get_my_assets()
            if symbol in assets:
                await asyncio.to_thread(exchange.create_market_sell_order, symbol, assets[symbol]['total'] * 0.5)
                await query.edit_message_text(f"🟠 {symbol} 50% 분할 매도 완료.")

        elif action == "adj_amt":
            try:
                adj_value = int(symbol)
                config.DEFAULT_TEST_BUY = max(5000, config.DEFAULT_TEST_BUY + adj_value)
                msg_text = query.message.text or ""
                if "매수포착" in msg_text or "매수권고" in msg_text:
                    try:
                        target_symbol = msg_text.split('] ')[1].split('\n')[0].strip()
                        is_auto = buy_individual_status.get(target_symbol) == 'AUTO'
                        new_kb = telegram_ui.get_buy_inline_kb(target_symbol, config.DEFAULT_TEST_BUY, is_auto)
                        await query.edit_message_reply_markup(reply_markup=new_kb)
                    except Exception as parse_e:
                        await query.edit_message_reply_markup(
                            reply_markup=telegram_ui.get_amt_kb(config.DEFAULT_TEST_BUY))
                else:
                    await query.edit_message_reply_markup(reply_markup=telegram_ui.get_amt_kb(config.DEFAULT_TEST_BUY))
                await query.answer(f"💰 설정 금액: {config.DEFAULT_TEST_BUY:,}원")
            except Exception as e:
                logger.error(f"❌ 금액 조정 오류: {e}")

        elif action == "set_amt":
            try:
                config.DEFAULT_TEST_BUY = int(symbol)
                await query.edit_message_reply_markup(reply_markup=telegram_ui.get_amt_kb(config.DEFAULT_TEST_BUY))
            except Exception as e:
                logger.error(f"❌ 프리셋 설정 오류: {e}")

        elif action == "toggle_all_sell_auto":
            current_all_auto = all(
                status == 'AUTO' for status in sell_mute_status.values()) if sell_mute_status else False
            new_status = 'WATCH' if current_all_auto else 'AUTO'
            assets = await get_my_assets()
            for sym in assets.keys(): sell_mute_status[sym] = new_status
            await query.answer("🤖 자동 전환 완료" if new_status == 'AUTO' else "⏳ 감시 전환 완료")
            await query.edit_message_reply_markup(reply_markup=telegram_ui.get_report_inline_kb(not current_all_auto))

        elif action == "set_all_sell_watch":
            assets = await get_my_assets()
            for sym in assets.keys():
                sell_mute_status[sym] = 'WATCH'
                if sym in pending_approvals: del pending_approvals[sym]
            await query.edit_message_text(f"{query.message.text}\n\n✅ 전종목 감시 모드 설정 완료")

        elif action == "reset_all_sell_status":
            sell_mute_status.clear()
            pending_approvals.clear()
            await query.edit_message_text(f"{query.message.text}\n\n✅ [알림] 모든 매도 설정 초기화 완료")

        elif action == "request_instant_report":
            await process_report_logic(update, context, query)

        elif action == "manage_asset":
            try:
                await query.edit_message_text(
                    text=f"⚙️ [{symbol}] 종목 관리 모드\n현재 상태를 변경하거나 즉시 매도할 수 있습니다.",
                    reply_markup=telegram_ui.get_report_manage_kb(symbol)
                )
            except Exception as e:
                logger.error(f"Manage Asset Error: {e}")

        elif action == "sell_now":
            assets = await get_my_assets()
            if symbol in assets:
                qty = float(assets[symbol]['total'])
                await asyncio.to_thread(exchange.create_market_sell_order, symbol, qty)
                await query.edit_message_text(f"🔴 [{symbol.split('/')[0]}] 즉시 매도를 집행했습니다.")
                if symbol in pending_approvals: del pending_approvals[symbol]
            else:
                await query.answer("보유 중인 종목이 아닙니다.")

        elif action == "mute_30m":
            sell_mute_status[symbol] = 'MUTE'
            await query.answer("30분간 알람을 중단합니다.")

        elif action == "set_pending_30m":
            if symbol in pending_approvals:
                limit = pending_approvals[symbol].get('wait_limit', 30)
                pending_approvals[symbol].update({
                    'status': 'WAITING',
                    'start_time': datetime.now(),
                    'wait_limit': limit
                })
                icon = "🚨" if limit == 10 else "🟡"
                await query.edit_message_text(
                    f"{icon} {symbol.split('/')[0]} 매도 유예 시작\n"
                    f"지금부터 {limit}분간 감시 후 자동 매도를 결정합니다.\n"
                    f"(수익률 +0.5% 회복 시 자동 취소)"
                )
            else:
                await query.answer("이미 처리되었거나 유효하지 않은 요청입니다.", show_alert=True)

    elif update.message and update.message.text:
        # 기존 텍스트 메시지 처리 로직 100% 유지
        if msg == "📊 실시간 리포트":
            await process_report_logic(update, context)
        elif "평균매수가" in msg:
            try:
                parts = msg.split()
                coin, price = parts[0].upper(), float(parts[2])
                sym = f"{coin}/KRW"
                # 기존 인벤토리 데이터를 불러와서 기존 등급/타입을 유지하도록 보강
                inv_data = load_inventory()
                existing_item = inv_data.get(sym, {})
                
                curr_grade = existing_item.get('grade', 'A')
                curr_type = existing_item.get('buy_type', 1)
                assets = await get_my_assets()
                qty = assets.get(sym, {}).get('total_quantity', 0)
                purchase_time=assets.get(sym, {}).get('purchase_time')
                save_inventory(sym, price, qty, curr_grade, curr_type, purchase_time)
                await update.message.reply_text(f"✅ {sym} 평단가 {price:,.0f}원 설정 완료")
            except:
                pass
        elif msg == "🤖 자동(신중)":
            config.buy_mute_mode = 'AUTO'
            GLOBAL_BUY_MODE = "SHIELD"
            print(f"✅ [DEBUG] 시스템 모드 변경: {config.buy_mute_mode} | 매수속도: {GLOBAL_BUY_MODE}")
            await update.message.reply_text("🛡️ [자동-신중] 10분 추적 후 매수합니다.")
        elif msg == "⚡ 자동(즉시)":
            config.buy_mute_mode = 'AUTO'
            GLOBAL_BUY_MODE = "LIGHTNING"
            print(f"✅ [DEBUG] 시스템 모드 변경: {config.buy_mute_mode} | 매수속도: {GLOBAL_BUY_MODE}")
            await update.message.reply_text("⚡ [자동-즉시] 대기 없이 즉시 매수합니다.")
        elif msg == "⏳ 감시 모드":
            config.buy_mute_mode = 'WATCH'
            await update.message.reply_text("🔍 [전체 제어] 감시 모드 활성화")
        elif msg == "🔄 모드 초기화":
            config.buy_mute_mode = None
            sell_mute_status.clear();
            buy_individual_status.clear()
            await update.message.reply_text("🔄 시스템 상태가 초기화되었습니다.")
        elif msg == "💰 금액설정":
            await update.message.reply_text("매수 단위 금액 선택:",
                                            reply_markup=telegram_ui.get_amt_kb(config.DEFAULT_TEST_BUY))


async def process_report_logic(update, context, query=None):
    """[최종 복구] 실시간 리포트 - 11개 전 종목 노출 + 수익률 정상화 + 흰색 제거"""
    current_mode = getattr(config, 'buy_mute_mode', 'MANUAL')
    is_manual_auto = (current_mode == 'AUTO')
    print(f"🔍 [DEBUG] 현재 메모리상의 buy_mute_mode 상태: {current_mode}")
    try:
        # [원본 로직] 자산 및 인벤토리 로드
        assets = await get_my_assets()
        inv_data = load_inventory()
        is_night = config.is_sleeping_time()
        is_manual_auto = (getattr(config, 'buy_mute_mode', 'MANUAL') == 'AUTO')
        ##### [수정] 정렬과 집계를 위해 딕셔너리 구조 리스트로 변경 #####
        report_data_list = []
        urgent_count = 0

        # [핵심] 필터링(continue) 없이 assets에 있는 모든 종목을 순회
        for symbol, data in assets.items():
            sym_only = symbol.split('/')[0]
            ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
            this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)
            
            # 가격 0원 시 보정 로직
            if this_curr_p == 0:
                # 30분봉 데이터를 미리 가져와서 종가 활용 (루프 내 ohlcv 호출부 활용 가능)
                ohlcv_fallback = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=1)
                if ohlcv_fallback:
                    this_curr_p = float(ohlcv_fallback[-1][4])
                else:
                    continue # 데이터가 아예 없으면 스킵
                
            inv_item = inv_data.get(symbol) or inv_data.get(sym_only) or {}

            # [수정] JSON에서 확실히 평단가를 가져오게 키 이름을 모두 체크
            # 사용자님이 저장한 키가 'price'든 'purchase_price'든 다 뒤집니다.
            this_avg_p = float(
                inv_item.get('price') or 
                inv_item.get('purchase_price') or 
                inv_item.get('avg_price') or 
                data.get('avg_price') or 0
            )
            
            this_qty = float(
                inv_item.get('total_quantity') or 
                inv_item.get('total') or 
                data.get('total') or 0
            )

            # [디버그 로그] 이제 JSON평단이 None인지 숫자인지 꼭 보세요.
            config.logger.info(f"🚨 [FINAL CHECK] {symbol} | JSON데이터유무:{bool(inv_item)} | 최종평단:{this_avg_p}")

            # 수익률 계산
            this_profit = ((this_curr_p - this_avg_p) / this_avg_p * 100) if this_avg_p > 0 else 0
            this_profit_krw = (this_curr_p - this_avg_p) * this_qty
            
            # 계산 결과 확인 로그
            config.logger.info(f"📊 [CALC RESULT] {symbol} | 수익률:{this_profit:.2f}% | 수익금:{this_profit_krw:,.0f}원")

            # 인벤토리 데이터 매칭 (등급 및 매수시간)
            this_grade = inv_item.get('grade', 'A')
            # 실시간 경과 시간 추출
            this_elapsed_bars = 0
            buy_time_str = inv_item.get('purchase_time')
            this_buy_type = inv_item.get('buy_type', 1)
            if buy_time_str:
                try:
                    buy_time_dt = datetime.strptime(buy_time_str, '%Y-%m-%d %H:%M:%S')
                    diff_sec = (datetime.now() - buy_time_dt).total_seconds()
                    this_elapsed_bars = int(diff_sec / 1800)  # 30분봉 기준
                except:
                    this_elapsed_bars = 99
            else:
                this_elapsed_bars = 99

            # 야간 모드 및 모드 아이콘 판정
            raw_status = sell_mute_status.get(symbol, 'WATCH')
            is_global_auto = is_night or is_manual_auto
            status = 'AUTO' if is_global_auto else raw_status

            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            ma40_line = df['close'].rolling(40).mean().iloc[-1]
            
            symbol_wo_quote = symbol.split('/')[0]
            asset_info = assets.get(symbol, {})
            purchase_price = float(asset_info.get('avg_buy_price', 0)) # 여기서 변수 정의!
            
            # 전략 엔진 호출
            is_sell_signal, sell_reason, is_urgent = await strategy.check_sell_signal(
                exchange=exchange,
                df=df,
                symbol=symbol,
                purchase_price=this_avg_p,
                max_price=float(inv_item.get('max_price', this_curr_p)),
                grade=this_grade,
                symbol_inventory_age=this_elapsed_bars,
                status=status,
                realtime_p=this_curr_p,
                buy_type=this_buy_type
            )
            # [추가: 3번 타입 방어 로직 - 정기 리포트와 동일하게 맞춤] #####
            
            if this_buy_type == 3:
                # [1순위] 절대 손절선 감시
                if this_profit <= -3.0:
                    is_sell_signal = True
                    sell_reason = "📉 [3번-절대손절] 매수가 대비 -3% 도달"
                else:
                    # 90선 무시
                    if is_sell_signal and "90선" in sell_reason:
                        is_sell_signal = False
                        sell_reason = ""
                    # 6봉 이전 40선 이탈 무시
                    if this_elapsed_bars < 6:
                        if is_sell_signal and "40선" in sell_reason:
                            is_sell_signal = False
                            sell_reason = ""
                    # 6봉 이후 사유 변경
                    else:
                        if is_sell_signal and "40선" in sell_reason:
                            sell_reason = "⚠️ [3번-유예종료] 6봉 경과 후 40선 이탈"
            # 비주얼 판정 (기존 로직 보존)
            if status == 'KEEP' and not (is_sell_signal and "0순위" in sell_reason):
                report_color, status_text, mode_str = "🟢", "유지 중", " 🔒"
                mode_icon = ""
            else:
                # [수정] pending_approvals에 없더라도 is_sell_signal이 True면 
                # 유예 로직을 시뮬레이션하여 색상을 결정합니다.
                temp_approvals = pending_approvals.copy()
                if is_sell_signal and symbol not in temp_approvals:
                    temp_approvals[symbol] = {
                        'status': 'NOTIFIED',
                        'start_time': datetime.now(),
                        'wait_limit': 10 if ("1순위" in sell_reason or "2음봉" in sell_reason) else 30
                    }

                report_color, status_text = strategy.get_report_visuals(
                    this_profit, is_sell_signal, this_curr_p, ma40_line,
                    sell_reason, symbol, temp_approvals
                )
                
                # ==============================================================================
                # [복구 내용 1: 자동 리포트와 동일한 모드 판별 및 아이콘 강제 적용]
                # ==============================================================================
                is_emergency = emergency_mode.get(symbol, 0) >= 1
                is_sell_pending = symbol in temp_approvals
                is_profit_timer = inv_item.get('profit_deadline') is not None
                
                if is_emergency:
                    mode_str, report_color = "긴급", "🚨"
                elif is_sell_pending:
                    mode_str, report_color = "유예", "🟡"
                elif is_profit_timer:
                    mode_str, report_color = "수익", "🔵"
                else:
                    mode_str = "일반"
                
                mode_icon = " 🤖" if status == 'AUTO' else ""

            # [복구 내용 2: 꼬여있던 리포트 출력 포맷 정상화]
            report_line = f"{report_color} [{this_grade} | {mode_str}] {symbol.split('/')[0]:<6} | {this_curr_p:,.0f}원 | {this_profit:+.2f}%({this_profit_krw:+,.0f}원) | {status_text}{mode_icon}"
            ##### [변경] 정렬을 위해 데이터 객체로 저장 #####
            report_data_list.append({
                'text': report_line,
                'profit': this_profit,
                'button': InlineKeyboardButton(f"🔍 {symbol.split('/')[0]}", callback_data=f"manage_asset:{symbol}")
            })

        ##### [추가] 1. 수익률 기준 내림차순 정렬 #####
        report_data_list.sort(key=lambda x: x['profit'], reverse=True)
        
        # 텍스트 라인과 버튼 리스트 추출
        final_text_lines = [item['text'] for item in report_data_list]
        symbol_buttons = [item['button'] for item in report_data_list]

        # ==============================================================================
        # [복구 내용 3: 🚨 카운트 요약본에 추가 누락분 복구]
        # ==============================================================================
        summary = ""
        if final_text_lines:
            summary = (
                f"🟢:{sum(1 for l in final_text_lines if '🟢' in l)} | "
                f"🔵:{sum(1 for l in final_text_lines if '🔵' in l)} | "
                f"🟡:{sum(1 for l in final_text_lines if '🟡' in l)} | "
                f"🔴:{sum(1 for l in final_text_lines if '🔴' in l)}"
            )
            urgent_count = sum(1 for l in final_text_lines if '🚨' in l)
            if urgent_count > 0:
                summary += f" | 🚨:{urgent_count}"
            summary += "\n"

        # [원본 로직] 하단 버튼 키보드 구성 (기능 유지)
        final_rows = [symbol_buttons[i:i + 4] for i in range(0, len(symbol_buttons), 4)]
        is_all_auto = all(sell_mute_status.get(s) == 'AUTO' for s in assets.keys()) if assets else False
        report_kb = telegram_ui.get_report_inline_kb(is_all_auto)
        if report_kb and hasattr(report_kb, 'inline_keyboard'):
            final_rows.extend(report_kb.inline_keyboard)

        # 최종 메시지 조립
        if is_night:
            mode_tag = " (야간 AUTO)"
        elif is_manual_auto:
            mode_tag = " (AUTO)"
        else:
            mode_tag = ""

        msg_text = f"📊 [실시간 리포트]{mode_tag}\n{summary}" + ("━━━━━━━━━━━━\n" + "\n".join(final_text_lines) if final_text_lines else "보유 종목 없음")

        # 전송 방식 분기 (수정 vs 신규)
        if query:
            await query.edit_message_text(text=msg_text, reply_markup=InlineKeyboardMarkup(final_rows))
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg_text,
                reply_markup=InlineKeyboardMarkup(final_rows)
            )

    except Exception as e:
        import traceback
        logger.error(f"Instant Report Error: {e}\n{traceback.format_exc()}")

async def is_sell_still_valid(symbol):
    """
    [7, 8, 9, 10번 통합] 매도 직전 최종 검증
    사용자님의 CCXT 환경에 맞춘 버전 (get_candles 오류 해결)
    """
    try:
        # 1. 현재가 및 캔들 데이터 직접 확보 (30분봉 기준)
        ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
        curr_p = float(ticker.get('last') or ticker.get('close') or 0)

        # get_candles 대신 직접 fetch_ohlcv 호출
        ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=50)
        if not ohlcv or curr_p == 0:
            return True, "데이터 부족으로 매도 진행"

        # 2. [8, 10번] 40일 이평선 회복 체크 (빨간 동그라미 방지)
        # ohlcv의 4번째 인덱스가 close(종가)입니다.
        closes = [x[4] for x in ohlcv]
        ma40 = sum(closes[-40:]) / 40

        # 현재가가 이미 40일선 위로 올라왔다면 '사유 해소'
        if curr_p > ma40:
            return False, f"현재가({curr_p:,.0f})가 40일선({ma40:,.0f}) 위로 회복됨"

        # 3. [7, 9번] 2음봉 사유가 지금도 유효한지 체크
        # 마지막 캔들의 시가(open)와 종가(close) 비교
        last_open = ohlcv[-1][1]
        last_close = ohlcv[-1][4]

        if last_close > last_open:
            return False, "현재 캔들이 양봉으로 반등 중"

        return True, "매도 조건 유지"

    except Exception as e:
        import logging
        logging.error(f"검증 로직 에러: {e}")
        return True, "에러 발생으로 안전 매도"


async def get_current_grade(exchange, symbol, df):
    """
    [최종] strategy.check_buy_signal 로직과 100% 동기화된 등급 판별
    """
    try:
        # check_buy_signal이 4개 값을 리턴하도록 변경됨: (is_buy, reason, grade, data_dict)
        is_buy, reason, grade, data_dict = await strategy.check_buy_signal(exchange, df, symbol, config.WARNING_LIST)

        if is_buy:
            # grade 값이 직접 반환됨 (예: "S+", "A+", "A", "S")
            if grade:
                # "S+" -> "S", "A+" -> "A"로 변환하여 반환
                if grade.startswith("S"): return "S"
                if grade.startswith("A"): return "A"
                return grade
            # grade가 없으면 reason에서 추출
            if "S급" in reason or "[S" in reason: return "S"
            if "A급" in reason or "[A" in reason: return "A"

        return "B"  # 그 외 일반 등급
    except Exception as e:
        logger.error(f"Grade check error: {e}")
        return "B"  # 에러 시 안전하게 자동매수 차단 등급 반환

###### [사용자 요청: 10분 유예 전담 마크 루프 신설] ######
async def pending_buy_task(app):
    """447개 스캔과 별개로 매수 대기 종목만 10초마다 체크하여 10분 정각에 집행"""
    global pending_s_buys
    while True:
        try:
            w_list = strategy.get_warning_list()
            now = datetime.now()
            for sym, info in list(pending_s_buys.items()):
                elapsed = (now - info['start_time']).total_seconds() / 60
                # [수정 시작] 3, 6, 9분 중간 보고 및 실시간 지표 이탈 감시 로직 삽입
                report_mark = int(elapsed // 3)
                if 0 < report_mark < 4 and report_mark > info.get('last_report_min', 0):
                    # 보고 시점에만 API 호출하여 부하 최소화
                    ohlcv_now = await asyncio.to_thread(exchange.fetch_ohlcv, sym, '30m', limit=300)
                    df_now = pd.DataFrame(ohlcv_now, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                    still_ok, now_reason, now_grade, _ = await strategy.check_buy_signal(exchange, df_now, sym, w_list)
                    
                    if still_ok:
                        await app.bot.send_message(config.CHAT_ID, f"⏳ [S급 추적] {sym} ({report_mark*3}분 경과)\n지표 양호 유지 중 (등급: {now_grade})")
                        info['last_report_min'] = report_mark
                    else:
                        await app.bot.send_message(config.CHAT_ID, f"⚠️ [S급 취소] {sym} 추적 중 지표 이탈\n사유: {now_reason}")
                        pending_s_buys.pop(sym, None)
                        continue
                # 10분 강제 집행 로직 (기존 buy_scan_task에서 분리됨)
                if elapsed >= 10:
                    ohlcv_final = await asyncio.to_thread(exchange.fetch_ohlcv, sym, '30m', limit=300)
                    df_final = pd.DataFrame(ohlcv_final, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                    is_still_good, final_reason, final_grade, final_data_dict = await strategy.check_buy_signal(exchange, df_final, sym, w_list)
                    extracted_type = "1"
                    if "TYPE1" in final_reason: extracted_type = "1"
                    elif "TYPE2" in final_reason: extracted_type = "2"
                    elif "TYPE3" in final_reason: extracted_type = "3"
                    elif "TYPE4" in final_reason: extracted_type = "4"
                    
                    if info.get('grade') == 'S' and final_grade == 'A':
                        final_grade = 'S' # 강등 방지 로직

                    if is_still_good:
                        buy_job = {
                            'symbol': sym, 
                            'cost': info['cost'], 
                            'grade': final_grade, 
                            'buy_type': extracted_type,
                            'reason': final_reason
                        }
                        buy_queue.put_nowait(buy_job)
                        config.logger.info(f"📥 [S급 정각 큐 예약] {sym} 매수 워커로 전달")
                    else:
                        # [추가] 매수 취소 알림: 10분 대기 중 지표 이탈 시 사용자에게 보고 (무단 잠수 방지)
                        await app.bot.send_message(config.CHAT_ID, f"⚠️ [매수취소] {sym} 지표 이탈로 10분 집행 포기 ({final_reason})")
                    
                    pending_s_buys.pop(sym, None)
            await asyncio.sleep(10) # 10초 간격으로 가볍게 실행
        except Exception as e:
            logger.error(f"Error in pending_buy_task: {e}")
            await asyncio.sleep(10)

async def buy_worker(app):
    config.logger.info("👷 [매수 워커 가동] 비차단 매수 시스템 시작")
    while True:
        job = await buy_queue.get()
        try:
            symbol = job['symbol']
            cost = job['cost']
            grade = job['grade']
            buy_type = job['buy_type']
            reason = job.get('reason', '알 수 없음') # [추가] Key 에러 방어
            
            success, msg = await safe_market_buy(symbol, cost, grade, buy_type)
            if success:
                # [수정] 큐 매수 완료 메시지에 reason 변수 추가
                await app.bot.send_message(config.CHAT_ID, f"🚀 [큐 집행 완료] {symbol}\n금액: {cost:,.0f}원\n사유: {reason}\n메시지: {msg}")
        except Exception as e:
            config.logger.error(f"❌ [Worker Error] {symbol} 매수 처리 중 오류: {e}")
        finally:
            buy_queue.task_done()

async def main():
    print("🚀 가상화폐 자동 매매 시스템 가동...")
    # 텔레그램 봇 설정
    app = Application.builder().token(config.TELEGRAM_TOKEN)\
        .connect_timeout(30).read_timeout(30).build()
    app.add_handler(CallbackQueryHandler(handle_interaction))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interaction))

    # [핵심] 매수 스캔과 매도 감시를 각각 독립된 비동기 타스크로 실행
    # 이제 buy_scan_task가 내부에서 3분을 쉬어도(await asyncio.sleep), 
    # sell_monitor_task는 전혀 방해받지 않고 자기 할 일을 합니다.
    buy_task = asyncio.create_task(buy_scan_task(app))
    sell_task = asyncio.create_task(sell_monitor_task(app))
    emergency_task = asyncio.create_task(emergency_monitor_task(app))
    pending_task = asyncio.create_task(pending_buy_task(app))
    worker_task = asyncio.create_task(buy_worker(app))
    # 텔레그램 인터페이스 시작
    await app.initialize()
    await app.start()
    await app.bot.send_message(config.CHAT_ID, "🚀 시스템 가동 시작", reply_markup=telegram_ui.get_main_keyboard())
    await app.updater.start_polling()

    try:
        # 두 타스크가 종료될 때까지 대기 (사실상 무한 루프)
        await asyncio.gather(buy_task, sell_task, emergency_task, pending_task)
    except Exception as e:
        logger.error(f"시스템 루프 에러 발생: {e}")
    finally:
        # 종료 시 안전하게 텔레그램 봇 정지
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 시스템을 종료합니다.")