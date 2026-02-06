import asyncio
import pandas as pd
import sys
import json
import os
import strategy, config, telegram_ui, analyzer
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from config import logger, exchange

# [전역 상태 관리] - 기존 로직 100% 유지 + 신규 토글 상태 반영
buy_mute_mode = None
sell_mute_status = {}  # [기능 19] 'AUTO' | 'WATCH'
buy_individual_status = {}  # 종목별 매수 개별 상태
pending_approvals = {}  # [기능 17] 무응답 자동 대응용
highest_rates = {}  # [기능 16] 수익 상승 보고용
last_report_time = datetime.now() - timedelta(days=1)
notified_symbols = {}
pending_approvals = {}
profit_alerts = {}
pending_s_buys = {}
# [사후분석] 미지 패턴 기록 종목의 60분 후 수익률 추적용 { symbol: (recorded_at, price_at_record) }
missed_60m_tracker = {}
# [추가] 비상 체제 상태를 저장할 딕셔너리 선언
emergency_mode = {}
# [평단가 로컬 관리용]
INV_FILE = "inventory.json"


def load_inventory():
    """저장된 인벤토리 파일을 불러옵니다."""
    if os.path.exists(INV_FILE):
        try:
            with open(INV_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Inventory Load Error: {e}")
            return {}
    return {}

def get_symbol_buy_type(symbol):
    """인벤토리에서 해당 종목의 buy_type을 직접 조회"""
    inv_data = load_inventory()  # 이미 main에 있는 함수 활용
    sym_only = symbol.split('/')[0]
    inv_item = inv_data.get(symbol) or inv_data.get(sym_only) or {}
    return inv_item.get('buy_type', 1)

def save_inventory(symbol, avg_price, quantity, grade="A", buy_type=1):
    """평단가, 수량, 그리고 [진입 등급]을 로컬 파일에 안전하게 저장합니다."""
    try:
        inv = load_inventory()
        # [수정] buy_time을 기록하여 strategy의 '6봉 유예' 로직과 연동
        # [추가] grade를 기록하여 실시간 리포트에서 진입 당시 등급 확인 가능
        inv[symbol] = {
            "avg_price": avg_price,      # 신규 로직용
            "purchase_price": avg_price, # 기존 호출부 호환용 (절대 삭제 금지)
            "total_quantity": quantity,
            "max_price": avg_price,      # 비상모드(고점관리) 초기값 자동 생성
            "grade": grade,
            "buy_type": buy_type,
            "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "purchase_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
        # [KRW 초과 방지] 수수료·슬리피지·호가 반올림 대비 85% 한도 (bithumb 주문량 초과 오류 방지)
        safe_cost = min(cost, int(free_krw * 0.85))
        if safe_cost < 1000:
            return False, "잔액 부족"

        # [수정 부분] Ticker 정보가 None인 경우를 대비한 방어 로직
        ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)

        # last가 없으면 close를, 그것도 없으면 info의 last_price를 시도
        curr_p = ticker.get('last') or ticker.get('close') or float(ticker.get('info', {}).get('last_price', 0))

        if not curr_p or curr_p == 0:
            return False, "현재가 조회 실패"

        curr_p = float(curr_p)
        ###### [수정] 호가(Orderbook) 기반 매수 조건 체크 (1호가 < 현재가 * 1.003) 및 재시도 로직
        try:
            # 1차 호가 조회
            orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
            asks = orderbook.get('asks', [])
            best_ask = float(asks[0][0]) if asks else curr_p

            # 조건 체크: 1호가가 현재가 대비 0.3% 이상 높으면 재시도
            if best_ask >= curr_p * 1.003:
                logger.info(f"⏳ {symbol} 1차 진입 유보: 호가 갭 과다 (1호가:{best_ask} >= 현재가:{curr_p}*1.003) -> 5초 대기")
                await asyncio.sleep(5)

                # 재시도: 호가 및 현재가 다시 조회 (완전히 새로 갱신)
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                curr_p = float(ticker.get('last') or ticker.get('close') or 0)
                orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
                asks = orderbook.get('asks', [])
                best_ask = float(asks[0][0]) if asks else curr_p

                # 2차 조건 체크
                if best_ask >= curr_p * 1.003:
                    logger.warning(f"❌ {symbol} 매수 포기: 호가 갭 지속 (1호가:{best_ask}, 현재가:{curr_p})")
                    return False, "호가 갭 과다로 매수 포기"
                    
        except Exception as e:
            logger.error(f"⚠️ 호가 체크 중 오류 발생 (매수 중단): {e}")
            return False, f"호가 체크 오류: {e}"
        # 수량 계산 (소수점 4자리 절사). 실제 체결금액이 safe_cost를 넘지 않도록 금액 기준으로 역산
        import math
        amount = math.floor((safe_cost / curr_p) * 10000) / 10000
        if amount <= 0:
            return False, "수량 계산 오류(금액/가격)"

        print(f"🛒 [매수집행] {symbol} | 금액: {safe_cost} | 수량: {amount} | 등급: {grade}")

        # 3. 시장가 매수 실행 (cost 파라미터로 주문 금액 상한 전달)
        order = await asyncio.to_thread(
            exchange.create_order,
            symbol,
            'market',
            'buy',
            amount,
            None,
            {'cost': safe_cost}
        )
        if not order or 'average' not in order or order['average'] is None:
            for _ in range(3):
                await asyncio.sleep(1)
                try:
                    order = await asyncio.to_thread(exchange.fetch_order, order['id'], symbol)
                    if order and 'average' in order and order['average']:
                        break
                except Exception as e:
                    logger.error(f"Fetch order retry failed: {e}")

        ###### [추가] 실 체결가 적용 (실패 시 curr_p 사용)
        real_price = order.get('average') if order and order.get('average') else curr_p

        # 4. 인벤토리 저장 로직 (기존 유지 + grade 인자 추가)
        inv = load_inventory()
        old = inv.get(symbol, {"avg_price": 0, "total_quantity": 0})
        old_p = float(old.get('avg_price', old.get('purchase_price', 0)))
        old_q = float(old.get('total_quantity', 0))
        
        final_avg = ((old_p * old_q) + (float(real_price) * amount)) / (old_q + amount)

        # [수정] 보강된 save_inventory를 호출하여 등급까지 저장
        save_inventory(symbol, final_avg, old_q + amount, grade, buy_type)

        return True, "성공"
    except Exception as e:
        logger.error(f"Market Buy Error ({symbol}): {e}")
        return False, str(e)


async def get_my_assets():
    """[수익률 해결] inventory.json(로컬)을 API보다 우선 참조하여 -100% 원천 차단"""
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        inv = load_inventory()
        assets = {}

        # 빗썸 API의 상세 데이터 추출 (보조용)
        raw_info = balance.get('info', {}).get('data', {})

        for coin, total_val in balance['total'].items():
            total = float(total_val)
            if total <= 0.0001 or coin == 'KRW':
                continue

            symbol = f"{coin}/KRW"

            # [교정 1순위] 로컬 인벤토리(inventory.json) 무조건 우선!
            # 사용자님이 직접 입력한 값이 있다면 API가 뭐라든 이 값을 씁니다.
            local_item = inv.get(symbol) or inv.get(coin) or {}
            avg_p = float(local_item.get('purchase_price') or local_item.get('avg_price') or local_item.get('avg_buy_price') or 0)

            # [교정 2순위] 로컬에 데이터가 없을 때만 API를 뒤집니다.
            if avg_p == 0:
                coin_info = raw_info.get(coin, {})
                try:
                    # 빗썸 API의 여러 평단가 필드 검색
                    avg_p = float(
                        coin_info.get('avg_buy_price') or
                        coin_info.get('avg_buy_price_all') or
                        coin_info.get('average_price') or
                        0
                    )
                except:
                    avg_p = 0

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

        return assets
    except Exception as e:
        logger.error(f"Asset Fetch Error: {e}")
        return {}


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
    global buy_mute_mode, notified_symbols, buy_individual_status, pending_s_buys, missed_60m_tracker
    while True:
        try:
            assets = await get_my_assets()
            owned_symbols = set(assets.keys())
            is_night = config.is_sleeping_time()
            w_list = strategy.get_warning_list()
            markets = await asyncio.to_thread(exchange.fetch_markets)
            current_display_mode = "AUTO (야간)" if is_night else (buy_mute_mode or "WATCH")

            krw_filtered = [
                m for m in markets
                if m['quote'] == 'KRW' and m['active']
                   and m['symbol'].split('/')[0] not in w_list
                   and m['symbol'] not in owned_symbols
            ]
            # 1. 시장 전체 종목 등락률 수집 및 Panic Filter 상태 업데이트
            all_tickers = await asyncio.to_thread(exchange.fetch_tickers)
            market_rates = [float(all_tickers[m['symbol']]['percentage']) for m in krw_filtered 
                            if m['symbol'] in all_tickers and all_tickers[m['symbol']].get('percentage') is not None]
            
            if market_rates:
                current_market_avg = sum(market_rates) / len(market_rates)
                await strategy.update_market_panic_status(current_market_avg)
                
                # 시장 현황 보고 (사용자 확인용)
                lock_status = "🚨 [LOCK]" if strategy.is_buy_locked else "✅ [NORMAL]"
                print(f"\n📊 [시장 현황] {lock_status} | 현재평균: {current_market_avg:+.2f}% | 기준점: {strategy.market_ref_rate:+.2f}%")
                if strategy.is_buy_locked:
                    print(f"   💡 해제까지: {current_market_avg:+.2f}% -> {strategy.market_ref_rate + 2.0:+.2f}% 필요")
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
                sys.stdout.write(f"\r▶ 스캔 중: [{idx + 1}/{len(krw_filtered)}] {symbol:<12}")
                sys.stdout.flush()

                await asyncio.sleep(0.05)
                # [예외 처리] 지원하지 않는 마켓(symbollist 미포함) 방어
                markets_dict = getattr(exchange, 'markets', None)
                if markets_dict is not None and symbol not in markets_dict:
                    logger.info(f"지원하지 않는 마켓: {symbol}")
                    continue

                ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=200)
                if len(ohlcv) < 185: continue

                df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                # [수급 돌파] 1분봉 거래량 20봉 평균 300% + 3분 내 3% 급등 체크용 (옵션: 1m 있으면 전략에 전달)
                df_1m = None
                try:
                    ohlcv_1m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '1m', limit=25)
                    if ohlcv_1m and len(ohlcv_1m) >= 21:
                        df_1m = pd.DataFrame(ohlcv_1m, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                except Exception:
                    pass
                is_buy, reason, grade, data_dict = strategy.check_buy_signal(df, symbol, w_list, df_1m)
                
                # [분석 봇] 매수하지 않더라도 탈락 사유·패턴태그·등급 포함 상세 수치 기록 (조건 1개라도 만족/3분 내 3% 급등 포함)
                current_price = float(df.iloc[-1]['close'])
                if not is_buy and reason:
                    analyzer.record_missed_opportunity(symbol, reason, current_price, data_dict)
                    # [사후분석] 기록된 종목 60분 후 수익률 로그 업데이트용 등록 (조건 만족/3%급등 포함 모든 미지 기록)
                    missed_60m_tracker[symbol] = (datetime.now(), current_price)

                if is_buy:
                    if symbol in notified_symbols and (datetime.now() - notified_symbols[symbol]) < timedelta(hours=1):
                        continue
                    notified_symbols[symbol] = datetime.now()

                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    free_krw = float(balance['free'].get('KRW', 0))
                    buy_cost = await get_buy_cost()
                    buy_type = get_symbol_buy_type(symbol)

                    # [개선] grade 값 우선 사용, 없으면 reason에서 추출
                    is_s_class_check = (grade and grade.startswith("S")) or any(x in reason for x in ["S급", "[S]", "[S+]"])
                    indiv_mode_check = buy_individual_status.get(symbol)
                    curr_mode_check = indiv_mode_check if indiv_mode_check else ("AUTO" if is_night else buy_mute_mode)

                    # [S급 추적 등록]
                    if is_s_class_check and curr_mode_check == "AUTO":
                        if symbol not in pending_s_buys:
                            pending_s_buys[symbol] = {
                                'start_time': datetime.now(),
                                'last_check_min': 0,
                                'reason': reason,
                                'cost': buy_cost
                            }
                            await app.bot.send_message(
                                config.CHAT_ID,
                                f"🔔 [S급 포착] 30분 자동매수 추적 시작\n종목: {symbol}\n사유: {reason}\n\n※ 10분마다 지표 재확인 후 30분 뒤 강제 매수합니다.",
                                reply_markup=telegram_ui.get_buy_inline_kb(symbol, buy_cost, False)
                            )

                    # [매수 집행/알림 로직]
                    indiv_mode = buy_individual_status.get(symbol)
                    curr_mode = indiv_mode if indiv_mode else ("AUTO" if is_night else buy_mute_mode)
                    #########################################################
                    # [수정] 등급 판정 및 타입별 자동 매수 필터링
                    # S+, S는 'S'로 / A+, A는 'A'로 통합 판정
                    # reason 문자열을 분석하여 실시간 등급(current_grade) 확정
                    if any(x in reason for x in ["S급", "[S]", "TYPE3", "Type 3"]):
                        current_grade = "S"
                    elif "A" in (grade or reason):
                        current_grade = "A"
                    else:
                        current_grade = "B"

                    can_auto_buy = False
                    if curr_mode == "AUTO":
                        if buy_type == 1:
                            if current_grade in ["S", "A"]: can_auto_buy = True
                        elif buy_type in [2, 3]:
                            if current_grade == "S": can_auto_buy = True

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

                            # 수정된 final_buy_cost로 매수 집행
                            success, msg = await safe_market_buy(symbol, final_buy_cost, current_grade)
                            if success:
                                display_grade = "A급" if "[A]" in reason or "A급" in reason else "S급"

                                await app.bot.send_message(
                                    config.CHAT_ID,
                                        f"🤖 [{display_grade} 즉시매수 완료] {symbol}\n"
                                        f"💡 사유: {reason}\n"
                                        f"💰 투입: {buy_cost:,.0f}원"
                                    )
                                if symbol in pending_s_buys: del pending_s_buys[symbol]
                    else:
                        status_tag = "💎 [매수포착 - A급]" if not is_s_class_check else "🔥 [S급 포착/수동대기]"
                        is_auto_btn = (indiv_mode == 'AUTO')
                        await app.bot.send_message(
                            config.CHAT_ID,
                            f"{status_tag} {symbol}\n💡 등급: {reason}\n💰 설정금액: {buy_cost:,.0f}원\n💳 가용잔액: {free_krw:,.0f}원",
                            reply_markup=telegram_ui.get_buy_inline_kb(symbol, buy_cost, is_auto_btn)
                        )

            # 2. S급 강제 매수 추적기 (스캔 루프 종료 후 독립 실행 - 들여쓰기 교정됨)
            # ---------------------------------------------------------
            for sym, info in list(pending_s_buys.items()):
                if sym in owned_symbols:
                    if sym in pending_s_buys: del pending_s_buys[sym]
                    continue

                elapsed = (datetime.now() - info['start_time']).total_seconds() / 60

                # 지표 재확인
                current_mark = int(elapsed // 10) * 10
                if 0 < current_mark < 30 and current_mark > info['last_check_min']:
                    ohlcv_now = await asyncio.to_thread(exchange.fetch_ohlcv, sym, '30m', limit=200)
                    df_now = pd.DataFrame(ohlcv_now, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                    still_buy, now_reason, now_grade, now_data_dict = strategy.check_buy_signal(df_now, sym, w_list)

                    if still_buy:
                        info['last_check_min'] = current_mark
                        await app.bot.send_message(config.CHAT_ID, f"ℹ️ [S급 추적] {sym} {current_mark}분 경과. 지표 양호 유지 중.")
                    else:
                        await app.bot.send_message(config.CHAT_ID, f"⚠️ [S급 취소] {sym} 지표 이탈로 자동 매수 대기를 취소합니다.")
                        if sym in pending_s_buys: del pending_s_buys[sym]
                        continue

                # 30분 강제 집행
                if elapsed >= 30:
                    ohlcv_final = await asyncio.to_thread(exchange.fetch_ohlcv, sym, '30m', limit=200)
                    df_final = pd.DataFrame(ohlcv_final, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
                    is_still_good, final_reason, final_grade, final_data_dict = strategy.check_buy_signal(df_final, sym, w_list)

                    if is_still_good:
                        success, msg = await safe_market_buy(sym, info['cost'], "S")
                        if success:
                            logger.info(f"REPORT_DATA|{sym}|S|{info['cost']}")
                            await app.bot.send_message(config.CHAT_ID, f"🤖 [S급 강제집행] 30분 경과 및 지표 유지로 자동 매수 완료: {sym}")
                        else:
                            await app.bot.send_message(config.CHAT_ID, f"❌ [강제집행 실패] {sym} 사유: {msg}")
                    else:
                        await app.bot.send_message(config.CHAT_ID, f"⚠️ [S급 취소] 30분 경과 시점 지표 부적합으로 취소합니다.")

                    if sym in pending_s_buys: del pending_s_buys[sym]

            print(f"\n✅ 스캔 완료 | {datetime.now().strftime('%H:%M:%S')}")
            await asyncio.sleep(300)

        except Exception as e:
            logger.error(f"Buy Task Error: {e}")
            await asyncio.sleep(60)

async def execute_sell(app, symbol, reason):
    """
    실제 거래소 매도 주문을 실행하고 사용자에게 알림을 보냅니다.
    """
    try:
        # [1] 실제 매도 실행 (이미 구현된 매도 로직이 있다면 그 함수를 호출)
        # 예: await exchange.create_market_sell_order(symbol, quantity)
        # 1. 현재 잔고 확인
        balance = await asyncio.to_thread(exchange.fetch_balance)
        base = symbol.split('/')[0]
        quantity = float(balance['free'].get(base, 0))

        # 2. 최소 주문 수량 체크 (잔고가 거의 없으면 무시)
        if quantity <= 0:
            logger.warning(f"⚠️ {symbol} 매도 실패: 잔고가 0입니다.")
            return

        # 3. 실제 시장가 매도 주문 던지기
        # 주문이 완료될 때까지 await로 기다립니다.
        order_result = await asyncio.to_thread(exchange.create_market_sell_order, symbol, quantity)
        
        logger.info(f"💰 {symbol} 매도 집행 완료: {reason} | 수량: {quantity}")

        inv = load_inventory()
        item = inv.get(symbol, {})
        avg_buy_price = float(item.get('avg_price', item.get('purchase_price', 0)))

        # 상위 3호가 중 최고가 및 0.3% 편차 확인 로직
        orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
        best_ask = max([float(a[0]) for a in orderbook['asks'][:3]])
        curr_p = float(orderbook['bids'][0][0])
        final_p = curr_p if (best_ask - curr_p) / curr_p >= 0.003 else best_ask

        sell_price = float(order_result.get('average') or order_result.get('price') or 0)
        
        # [추가] 수익률 계산 (평단가가 있을 때만)
        this_profit = 0.0
        if avg_buy_price > 0 and sell_price > 0:
            this_profit = ((sell_price - avg_buy_price) / avg_buy_price) * 100
        
        # [2] 텔레그램 알림
        await app.bot.send_message(
            config.CHAT_ID, 
            f"💰 [매도 완료] {symbol}\n사유: {reason} | 📊 최종 수익률: {this_profit:+.2f}%"
        )
        
        # [3] 유예 목록에서 제거
        if symbol in pending_approvals:
            del pending_approvals[symbol]
            
    except Exception as e:
        logger.error(f"❌ {symbol} 매도 집행 중 에러: {e}")

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

async def sell_monitor_task(app):
    """[최종 복구] 기존 유예/취소/0순위 로직 완전 유지 + 수익률 & 야간 모드 보정"""
    global last_report_time, sell_mute_status, pending_approvals, profit_alerts, emergency_mode
    while True:
        try:
            # 기본 대기 시간 1분
            current_loop_wait_time = 60
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
                # 인벤토리 데이터 미리 로드 (평단가 보충 및 등급 확인용)
                # 인벤토리 데이터 미리 로드
                inv_item = inv_data.get(symbol) or inv_data.get(symbol.split('/')[0]) or {}
                
                # [데이터 출처 추적]
                p_inv = inv_item.get('price') or inv_item.get('purchase_price') or inv_item.get('avg_price')
                p_exch = data.get('avg_price')
                
                # 1순위: 우리 인벤토리 기록, 2순위: 거래소 데이터
                this_avg_p = float(p_inv or p_exch or 0)
                this_qty = float(data.get('total') or inv_item.get('total_quantity') or 0)

                # [분석용 로그] 사자마자 팔리는 원인을 잡기 위해 무조건 출력
                print(f"🔍 [MONITOR] {symbol} | 현재가: {this_curr_p:,.0f} | 평단가: {this_avg_p:,.0f} (기록:{p_inv} / 거래소:{p_exch})")

                if this_avg_p <= 0:
                    # 평단가가 없으면 일단 '현재가'를 평단가로 가정해서 수익률을 0%로 만듦 (강제 매도 방지)
                    print(f"⚠️ [WARN] {symbol} 평단가 0원 -> 현재가({this_curr_p:,.0f})로 임시 대체")
                    this_avg_p = this_curr_p

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
                    save_inventory(symbol, this_avg_p, this_qty) # 고점 갱신 시 저장

                if this_profit >= 10.0 or emergency_mode.get(symbol, False):
                    current_loop_sleep = 30
                ##############################################
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
                    this_elapsed_bars = 999
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
                    symbol_inventory_age=this_elapsed_bars,
                    status=status, realtime_p=realtime_price
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
                    
                    # 1. 즉시 시장가 매도 실행 (유예 리스트 등록 과정 생략)
                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    base = symbol.split('/')[0]
                    free_qty = float(balance['free'].get(base, 0))
                    sell_qty = min(this_qty, free_qty)
                    
                    if sell_qty > 0:
                        # 호가창 분석 후 가격 결정
                        orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
                        bids = orderbook.get('bids', [])
                        if len(bids) >= 3:
                            top_bid_p = float(bids[0][0])
                            gap_ratio = abs(this_curr_p - top_bid_p) / this_curr_p
                            # 갭 0.3% 미만이면 1호가, 이상이면 현재가 -1호가
                            sell_price = top_bid_p if gap_ratio < 0.003 else get_tick_size(this_curr_p, direction='down')
                            order_result = await asyncio.to_thread(exchange.create_limit_sell_order, symbol, sell_qty, sell_price)
                        else:
                            order_result = await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                        
                        if order_result and 'id' in order_result:
                            save_trade_log(symbol, this_grade, this_avg_p, this_curr_p, this_profit, f"[긴급]{sell_reason}")
                            # 2. 자산 목록에서 즉시 제거 및 알림
                            if symbol in assets: del assets[symbol]
                            if symbol in pending_approvals: del pending_approvals[symbol]
                            
                            await app.bot.send_message(
                                config.CHAT_ID, 
                                f"🔥 [긴급 즉시 매도 완료]\n종목: {symbol}\n사유: {sell_reason}\n현재수익: {this_profit:+.2f}%"
                            )
                            logger.info(f"✅ {symbol} 긴급 매도 성공")
                            continue # 다음 종목으로 점프
                    else:
                        logger.error(f"❌ {symbol} 긴급 매도 실패: 잔고 부족")
                ############################################################################

                # 추가 로직: 매수 초기(6봉 미만) 90선 이탈 신호 강제 무시
                if is_sell_signal and (this_grade == 'S' or this_buy_type == 3):
                    if any(x in sell_reason for x in ["90선", "40선", "지지선"]) and this_profit < 3.0:
                        is_sell_signal = False
                        sell_reason = ""

                # [추가 로직: 3번 타입 하락 후 상승 종목 전용 방어막] #####
                if this_buy_type == 3:
                    # [1순위] 절대 손절선 감시 (6봉 여부와 상관없이 항상 작동)
                    if this_profit <= -3.0:
                        is_sell_signal = True
                        sell_reason = "📉 [3번-절대손절] 매수가 대비 -3% 도달"
                    
                    # [2순위] 유예 기간 및 40선 감시
                    else:
                        # A. 90선 관련 신호는 3번 타입에선 항상 무시
                        if is_sell_signal and "90선" in sell_reason:
                            is_sell_signal = False
                            sell_reason = ""

                        # B. 6봉(3시간) 이전일 때
                        if this_elapsed_bars < 6:
                            # 40선 이탈 신호가 오더라도 무조건 False로 꺾어서 버팀
                            if is_sell_signal and "40선" in sell_reason:
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

                elapsed_min = 0
                if is_sell_signal:
                    # [1] 긴급 매도(is_urgent) 확인
                    # strategy.py에서 True로 넘어온 경우 유예 없이 바로 True 처리
                    if is_urgent:
                        is_sell_final = True
                        # 긴급 매도는 유예 리스트에 넣지 않고 즉시 로직 아래에서 처리되게 합니다.
                        
                    # [2] 일반 매도 (유예 시스템 작동)
                    elif symbol not in pending_approvals:
                        # 모든 일반 매도 사유에 대해 유예 시간을 10분으로 통합
                        wait_limit = 10 
                        
                        # 텔레그램 알림 아이콘 설정 (긴박함을 알리기 위해 🚨 사용)
                        icon = "🚨" 
                        
                        # 유예 리스트 등록
                        pending_approvals[symbol] = {
                            'status': 'NOTIFIED',
                            'start_time': datetime.now(),
                            'entry_profit': this_profit,
                            'reason': sell_reason,
                            'wait_limit': wait_limit
                        }
                        
                        # 텔레그램 알림 발송
                        kb = telegram_ui.get_sell_signal_kb(symbol, wait_limit)
                        await app.bot.send_message(
                            config.CHAT_ID,
                            f"{icon} [{wait_limit}분 유예 시작] {symbol}\n"
                            f"사유: {sell_reason}\n"
                            f"현재수익률: {this_profit:+.2f}% | 현재가: {this_curr_p:,.0f}원\n"
                            f"⏱ 대응 선택 대기 (10분 뒤 자동 매도)", 
                            reply_markup=kb
                        )

                        pending_approvals[symbol] = {
                            'status': 'NOTIFIED',
                            'start_time': datetime.now(),
                            'entry_profit': this_profit,
                            'reason': sell_reason,
                            'wait_limit': wait_limit
                        }
                    else:
                        wait_data = pending_approvals[symbol]
                        # [기존 로직] 수익률 회복 시 유예 취소
                        if this_profit > wait_data.get('entry_profit', 0) + 0.5:
                            del pending_approvals[symbol]
                            await app.bot.send_message(config.CHAT_ID, f"✅ [매도 취소] {symbol} 수익률 회복")
                        elif wait_data.get('status') in ['WAITING', 'NOTIFIED']:
                            elapsed_min = (datetime.now() - wait_data['start_time']).total_seconds() / 60
                            current_limit = wait_data.get('wait_limit', 30)
                            if elapsed_min >= current_limit:
                                is_sell_final = True
                                # [추가] 자동 모드일 경우 여기서 직접 매도 호출
                                if sell_mute_status.get(symbol) == 'AUTO':
                                    orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
                                    bids = orderbook.get('bids', [])
                                    sell_price = this_curr_p # 기본값
                                    if len(bids) >= 3:
                                        top_bid_p = float(bids[0][0])
                                        gap_ratio = abs(this_curr_p - top_bid_p) / this_curr_p
                                        sell_price = top_bid_p if gap_ratio < 0.003 else get_tick_size(this_curr_p, direction='down')
                                    
                                    # execute_sell 내부가 시장가라면, 여기서 직접 limit으로 쏘거나 execute_sell을 수정해야 함
                                    await asyncio.to_thread(exchange.create_limit_sell_order, symbol, this_qty, sell_price)
                                    ###### [ADD START] 매도 결과 알림 및 CSV 기록 ######
                                    sell_final_p = this_curr_p  # 시장가 매도이므로 현재가 기준
                                    final_reason = wait_data.get('reason') or "유예 종료 자동 매도"
                                    save_trade_log(symbol, this_grade, this_avg_p, sell_final_p, this_profit, final_reason)
                                    
                                    finish_msg = (
                                        f"✅ **[자동 매도 완료]**\n"
                                        f"종목: {symbol} (등급: {this_grade})\n"
                                        f"사유: {wait_data.get('reason', '유예 종료')}\n"
                                        f"최종수익률: **{this_profit:+.2f}%**\n"
                                        f"결과가 CSV에 기록되었습니다."
                                    )
                                    await app.bot.send_message(config.CHAT_ID, finish_msg, parse_mode='Markdown')
                                    ###### [ADD END] ######
                                    if symbol in pending_approvals: del pending_approvals[symbol]
                                    continue

                else:
                    if symbol in pending_approvals: del pending_approvals[symbol]

                # 4단계: 리포트 라인 생성 (등급 및 아이콘 복구)
                if status == 'KEEP' and not (is_sell_signal and "0순위" in sell_reason):
                    report_color = "🟢"
                    status_text = "유지 중"
                    mode_icon = " 🔒"
                else:
                    report_color, status_text = strategy.get_report_visuals(
                        this_profit, is_sell_signal, this_curr_p, ma40_line,
                        sell_reason, symbol, pending_approvals
                    )
                    mode_icon = " 🤖" if status == 'AUTO' else ""

                # [최종 출력] 등급 포함 한 줄 구성
                report_line = f"{report_color} [{this_grade}] {symbol.split('/')[0]:<6} | {this_curr_p:,.0f}원 | {this_profit:+.2f}%({this_profit_krw:+,.0f}원) | {status_text}{mode_icon}"
                ##### [수정/추가] 정렬을 위해 딕셔너리 형태로 데이터를 임시 저장합니다. #####
                report_lines.append({
                    'text': report_line,
                    'profit': this_profit,
                    'button': InlineKeyboardButton(f"🔍 {symbol.split('/')[0]}", callback_data=f"manage_asset:{symbol}")
                })

                # 5단계: 최종 집행
                # 감시 루프 하단부
                if is_sell_final:
                    # 이미 위에서 execute_sell을 했다면 중복 실행 방지 로직 필요
                    await execute_sell(app, symbol, sell_reason)
                    if symbol in pending_approvals: del pending_approvals[symbol]
                    if status == 'AUTO' or is_night or "0순위" in sell_reason:
                        balance = await asyncio.to_thread(exchange.fetch_balance)
                        base = symbol.split('/')[0]
                        free_qty = float(balance['free'].get(base, 0))
                        sell_qty = min(this_qty, free_qty)
                        if sell_qty <= 0:
                            logger.info(f"매도 건너뜀(잔고 부족): {symbol}")
                        else:
                            # [사후분석] 손절 시 직전 1분 봉(하락 속도) 수집 후 매도 실행
                            last_1m_open, last_1m_close = None, None
                            if this_profit < 0:
                                try:
                                    ohlcv_1m = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '1m', limit=3)
                                    if ohlcv_1m and len(ohlcv_1m) >= 2:
                                        last_1m_open = float(ohlcv_1m[-2][1])
                                        last_1m_close = float(ohlcv_1m[-2][4])
                                except Exception:
                                    pass
                            orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol)
                            bids = orderbook.get('bids', [])
                            if len(bids) >= 3:
                                top_bid_p = float(bids[0][0])
                                gap_ratio = abs(this_curr_p - top_bid_p) / this_curr_p
                                sell_price = top_bid_p if gap_ratio < 0.003 else get_tick_size(this_curr_p, direction='down')
                                order_result = await asyncio.to_thread(exchange.create_limit_sell_order, symbol, sell_qty, sell_price)
                            else:
                                order_result = await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                            ######### [신규 추가 시작: 매도 성공 시 중복 알람 차단 로직] #########
                            # 1. 주문 성공 여부 확인 (id가 있으면 성공)
                            if order_result and 'id' in order_result:
                                # [추가] 실제 체결 가격 확인 (없으면 현재가)
                                exec_price = float(order_result.get('average') or order_result.get('price') or this_curr_p)
                                
                                # [추가] CSV 기록 및 최종 알림
                                save_trade_log(symbol, this_grade, this_avg_p, exec_price, this_profit, sell_reason)
                                
                                finish_msg = (
                                    f"🔴 **[매도 완료]**\n"
                                    f"종목: {symbol} (등급: {this_grade})\n"
                                    f"사유: {sell_reason}\n"
                                    f"수익률: **{this_profit:+.2f}%** | 매도가: {exec_price:,.0f}원"
                                )
                                await app.bot.send_message(config.CHAT_ID, finish_msg, parse_mode='Markdown')
                                # 2. 감시 목록(assets)에서 즉시 제거 (이게 있어야 아래쪽 알람이 안 뜸)
                                if symbol in assets:
                                    del assets[symbol]
                                    logger.info(f"✅ {symbol} 매도 성공 확인: assets에서 제거됨")

                                # 3. 매도 성공 알림 (기존에 아래 있던 메시지 코드를 이 안으로 이동)
                                await app.bot.send_message(config.CHAT_ID, f"🔴 [매도 집행]\n{symbol} | 사유: {sell_reason} | 📊 최종 수익률: {this_profit:+.2f}%")

                                # 4. 이번 종목 처리는 끝났으니 즉시 다음 종목으로 (아래쪽 '긴급 권고' 로직 스킵)
                                continue 

                            else:
                                # 매도 주문이 실패했을 경우의 로그 (선택 사항)
                                logger.error(f"❌ {symbol} 매도 주문 실패 또는 응답 없음: {order_result}")
                            exec_price = float(order_result.get('average') or order_result.get('price') or this_curr_p)
                            if this_profit < 0 and this_avg_p and this_avg_p > 0:
                                target_stop = this_avg_p * 0.98
                                slippage_pct = (exec_price - target_stop) / target_stop * 100
                                analyzer.record_loss_review(symbol, exec_price, target_stop, slippage_pct, last_1m_open, last_1m_close)
                            if symbol in pending_approvals: del pending_approvals[symbol]
                    else:
                        limit = pending_approvals.get(symbol, {}).get('wait_limit', 30)
                        if elapsed_min >= limit:
                            await app.bot.send_message(
                                config.CHAT_ID,
                                f"🚨🚨 [긴급 매도 권고] {symbol}\n"
                                f"유예 시간이 {int(elapsed_min)}분 경과했습니다!\n"
                                f"직접 판단해 주세요! 🔔"
                            )

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
                    msg_text = (
                        f"📊 [정기 리포트] ({now_str}){' (야간 AUTO)' if is_night else ''}\n"
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
    log_file = "trade_history.csv"
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
    global buy_mute_mode, sell_mute_status, buy_individual_status, pending_s_buys
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
                from main import get_current_grade  # 참조 확인
                current_grade = get_current_grade(symbol, df)
                cost = config.DEFAULT_TEST_BUY if action == "buy_now" else 1000000

                print(f"📍 [수동매수 시작] {symbol} | 등급: {current_grade} | 금액: {cost}")
                # 변수에 담긴 현재 등급을 전달
                success, res_msg = await safe_market_buy(symbol, cost, current_grade)

                if success:
                    display_msg = f"🚀 [{symbol.split('/')[0]}] 매수 성공! (금액: {cost:,}원)"
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
            from main import process_report_logic
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
            from main import process_report_logic
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
                save_inventory(sym, price, qty, curr_grade, curr_type)
                await update.message.reply_text(f"✅ {sym} 평단가 {price:,.0f}원 설정 완료")
            except:
                pass
        elif msg == "🤖 자동 매매":
            buy_mute_mode = 'AUTO'
            await update.message.reply_text("🚀 [전체 제어] 자동 매매 활성화")
        elif msg == "⏳ 감시 모드":
            buy_mute_mode = 'WATCH'
            await update.message.reply_text("🔍 [전체 제어] 감시 모드 활성화")
        elif msg == "🔄 모드 초기화":
            buy_mute_mode = None
            sell_mute_status.clear();
            buy_individual_status.clear()
            await update.message.reply_text("🔄 시스템 상태가 초기화되었습니다.")
        elif msg == "💰 금액설정":
            await update.message.reply_text("매수 단위 금액 선택:",
                                            reply_markup=telegram_ui.get_amt_kb(config.DEFAULT_TEST_BUY))


async def process_report_logic(update, context, query=None):
    """[최종 복구] 실시간 리포트 - 11개 전 종목 노출 + 수익률 정상화 + 흰색 제거"""
    global pending_approvals, sell_mute_status

    try:
        # [원본 로직] 자산 및 인벤토리 로드
        assets = await get_my_assets()
        inv_data = load_inventory()
        is_night = config.is_sleeping_time()

        ##### [수정] 정렬과 집계를 위해 딕셔너리 구조 리스트로 변경 #####
        report_data_list = []
        urgent_count = 0

        # [핵심] 필터링(continue) 없이 assets에 있는 모든 종목을 순회
        for symbol, data in assets.items():
            ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
            this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)
            if this_curr_p == 0: continue
            sym_only = symbol.split('/')[0] # 'LSK/KRW' -> 'LSK'
            # 1순위: LSK/KRW, 2순위: LSK
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
            if buy_time_str:
                try:
                    buy_time_dt = datetime.strptime(buy_time_str, '%Y-%m-%d %H:%M:%S')
                    diff_sec = (datetime.now() - buy_time_dt).total_seconds()
                    this_elapsed_bars = int(diff_sec / 1800)  # 30분봉 기준
                except:
                    this_elapsed_bars = 999
            else:
                this_elapsed_bars = 999

            # 야간 모드 및 모드 아이콘 판정
            raw_status = sell_mute_status.get(symbol, 'WATCH')
            status = 'AUTO' if is_night else raw_status

            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            ma40_line = df['close'].rolling(40).mean().iloc[-1]
            
            symbol_wo_quote = symbol.split('/')[0]
            asset_info = assets.get(symbol_wo_quote, {})
            purchase_price = float(asset_info.get('avg_buy_price', 0)) # 여기서 변수 정의!
            
            # 전략 엔진 호출
            is_sell_signal, sell_reason, is_urgent = await strategy.check_sell_signal(
                exchange, df, symbol, purchase_price, symbol_inventory_age if 'symbol_inventory_age' in locals() else 0, # 있으면 쓰고 없으면 0
                status if 'status' in locals() else 'NORMAL' # 있으면 쓰고 없으면 NORMAL
            )
            # [추가: 3번 타입 방어 로직 - 정기 리포트와 동일하게 맞춤] #####
            
            this_buy_type = inv_item.get('buy_type', 1)
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
            else:
                # [수정] pending_approvals에 없더라도 is_sell_signal이 True면 
                # 유예 로직을 시뮬레이션하여 색상을 결정합니다.
                
                # 만약 지금 신호는 왔는데 아직 정기 루프가 등록을 안 했다면?
                # 가상의 대기 데이터를 만들어 visuals 함수에 전달합니다.
                temp_approvals = pending_approvals.copy()
                if is_sell_signal and symbol not in temp_approvals:
                    temp_approvals[symbol] = {
                        'status': 'NOTIFIED',
                        'start_time': datetime.now(),
                        'wait_limit': 10 if ("1순위" in sell_reason or "2음봉" in sell_reason) else 30
                    }

                report_color, status_text = strategy.get_report_visuals(
                    this_profit, is_sell_signal, this_curr_p, ma40_line,
                    sell_reason, symbol, temp_approvals # 보정된 approvals 전달
                )
                mode_str = " 🤖" if status == 'AUTO' else ""

            if report_color == "🚨": urgent_count += 1

            # [기존 출력 포맷 유지]
            report_line = f"{report_color} [{this_grade}] {symbol.split('/')[0]:<6} | {this_curr_p:,.0f}원 | {this_profit:+.2f}%({this_profit_krw:+,.0f}원) | {status_text}{mode_str}"
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

        ##### [추가] 2. 요약 집계 생성 (초-파-노-빨 순서) #####
        summary = ""
        if final_text_lines:
            summary = (
                f"🟢:{sum(1 for l in final_text_lines if '🟢' in l)} | "
                f"🔵:{sum(1 for l in final_text_lines if '🔵' in l)} | "
                f"🟡:{sum(1 for l in final_text_lines if '🟡' in l)} | "
                f"🔴:{sum(1 for l in final_text_lines if '🔴' in l)}\n"
            )

        # [원본 로직] 하단 버튼 키보드 구성 (기능 유지)
        final_rows = [symbol_buttons[i:i + 4] for i in range(0, len(symbol_buttons), 4)]
        is_all_auto = all(sell_mute_status.get(s) == 'AUTO' for s in assets.keys()) if assets else False
        report_kb = telegram_ui.get_report_inline_kb(is_all_auto)
        if report_kb and hasattr(report_kb, 'inline_keyboard'):
            final_rows.extend(report_kb.inline_keyboard)

        # 최종 메시지 조립
        night_tag = " (야간 AUTO)" if is_night else ""
        msg_text = f"📊 [실시간 리포트]{night_tag}\n{summary}" + ("━━━━━━━━━━━━\n" + "\n".join(final_text_lines) if final_text_lines else "보유 종목 없음")

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


def get_current_grade(symbol, df):
    """
    [최종] strategy.check_buy_signal 로직과 100% 동기화된 등급 판별
    """
    try:
        # check_buy_signal이 4개 값을 리턴하도록 변경됨: (is_buy, reason, grade, data_dict)
        is_buy, reason, grade, data_dict = strategy.check_buy_signal(df, symbol, config.WARNING_LIST)

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

async def main():
    print("🚀 가상화폐 자동 매매 시스템 가동...")
    
    # 텔레그램 봇 설정
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_interaction))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interaction))

    # [핵심] 매수 스캔과 매도 감시를 각각 독립된 비동기 타스크로 실행
    # 이제 buy_scan_task가 내부에서 3분을 쉬어도(await asyncio.sleep), 
    # sell_monitor_task는 전혀 방해받지 않고 자기 할 일을 합니다.
    buy_task = asyncio.create_task(buy_scan_task(app))
    sell_task = asyncio.create_task(sell_monitor_task(app))

    # 텔레그램 인터페이스 시작
    await app.initialize()
    await app.start()
    await app.bot.send_message(config.CHAT_ID, "🚀 시스템 가동 시작", reply_markup=telegram_ui.get_main_keyboard())
    await app.updater.start_polling()

    try:
        # 두 타스크가 종료될 때까지 대기 (사실상 무한 루프)
        await asyncio.gather(buy_task, sell_task)
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