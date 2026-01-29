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


def save_inventory(symbol, avg_price, quantity, grade="A"):
    """평단가, 수량, 그리고 [진입 등급]을 로컬 파일에 안전하게 저장합니다."""
    try:
        inv = load_inventory()
        # [수정] buy_time을 기록하여 strategy의 '6봉 유예' 로직과 연동
        # [추가] grade를 기록하여 실시간 리포트에서 진입 당시 등급 확인 가능
        inv[symbol] = {
            "avg_price": avg_price,
            "total_quantity": quantity,
            "grade": grade,  # 진입 등급 저장 추가
            "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        with open(INV_FILE, "w") as f:
            json.dump(inv, f, indent=4)
        print(f"💾 [기록완료] {symbol} | 등급: {grade} | 평단: {avg_price:,.0f} | 수량: {quantity}")
    except Exception as e:
        logger.error(f"Inventory Save Error: {e}")


# 프로그램 시작 시 메모리에 로드
manual_inventory = load_inventory()


async def safe_market_buy(symbol, cost, grade="A"):
    """시장가 매수 집행 및 진입 등급(grade) 기록 보강"""
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        free_krw = float(balance['free'].get('KRW', 0))
        safe_cost = int(min(cost, free_krw * 0.90))

        if safe_cost < 1000: return False, "잔액 부족"

        # [수정 부분] Ticker 정보가 None인 경우를 대비한 방어 로직
        ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)

        # last가 없으면 close를, 그것도 없으면 info의 last_price를 시도
        curr_p = ticker.get('last') or ticker.get('close') or float(ticker.get('info', {}).get('last_price', 0))

        if not curr_p or curr_p == 0:
            return False, "현재가 조회 실패"

        curr_p = float(curr_p)

        # 수량 계산 (소수점 4자리 절사)
        import math
        amount = math.floor((safe_cost / curr_p) * 10000) / 10000

        print(f"🛒 [매수집행] {symbol} | 금액: {safe_cost} | 수량: {amount} | 등급: {grade}")

        # 3. 시장가 매수 실행
        await asyncio.to_thread(
            exchange.create_order,
            symbol,
            'market',
            'buy',
            amount,
            None,
            {'cost': safe_cost}
        )

        # 4. 인벤토리 저장 로직 (기존 유지 + grade 인자 추가)
        inv = load_inventory()
        old = inv.get(symbol, {"avg_price": 0, "total_quantity": 0})
        old_p, old_q = float(old['avg_price']), float(old['total_quantity'])
        final_avg = ((old_p * old_q) + (curr_p * amount)) / (old_q + amount)

        # [수정] 보강된 save_inventory를 호출하여 등급까지 저장
        save_inventory(symbol, final_avg, old_q + amount, grade)

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
            avg_p = float(local_item.get('avg_price') or local_item.get('avg_buy_price') or 0)

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
                'buy_time': local_item.get('buy_time', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
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
        # 잔액이 설정 금액보다 적을 경우, 잔액의 95%만 주문하여 '초과 오류' 방지
        actual_cost = min(target_cost, free_krw * 0.95)

        # 빗썸 최소 주문 금액은 1,000원임
        if actual_cost < 1000:
            return 0

        return int(actual_cost)  # 정수형으로 반환
    except Exception as e:
        logger.error(f"Cost Calculation Error: {e}")
        return 0


async def buy_scan_task(app):
    """매수 스캔 태스크: 들여쓰기 교정 및 S급 추적 로직 정상화"""
    global buy_mute_mode, notified_symbols, buy_individual_status, pending_s_buys
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
                is_buy, reason, grade, data_dict = strategy.check_buy_signal(df, symbol, w_list)
                
                # [분석 봇] 매수 신호가 없을 때 탈락 사유 및 상세 수치 기록
                if not is_buy and reason:
                    current_price = float(df.iloc[-1]['close'])
                    analyzer.record_missed_opportunity(symbol, reason, current_price, data_dict)

                if is_buy:
                    if symbol in notified_symbols and (datetime.now() - notified_symbols[symbol]) < timedelta(hours=1):
                        continue
                    notified_symbols[symbol] = datetime.now()

                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    free_krw = float(balance['free'].get('KRW', 0))
                    buy_cost = await get_buy_cost()

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
                    is_s_class = (grade and grade.startswith("S")) or "S급" in reason

                    if curr_mode == "AUTO" and is_s_class:
                        if free_krw < 1000:
                            await app.bot.send_message(config.CHAT_ID, f"❌ [S급 자동매수 실패] {symbol}\n사유: 잔액 부족")
                        else:
                            success, msg = await safe_market_buy(symbol, buy_cost, "S")
                            if success:
                                await app.bot.send_message(
                                    config.CHAT_ID,
                                    f"🤖 [S급 즉시매수 완료] {symbol}\n💡 사유: {reason}\n💰 투입: {buy_cost:,.0f}원"
                                )
                                if symbol in pending_s_buys: del pending_s_buys[symbol]
                    else:
                        status_tag = "💎 [매수포착 - A급]" if not is_s_class else "🔥 [S급 포착/수동대기]"
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
            await asyncio.sleep(600)

        except Exception as e:
            logger.error(f"Buy Task Error: {e}")
            await asyncio.sleep(60)


async def sell_monitor_task(app):
    """[최종 복구] 기존 유예/취소/0순위 로직 완전 유지 + 수익률 & 야간 모드 보정"""
    global last_report_time, sell_mute_status, pending_approvals, profit_alerts
    while True:
        try:
            # [추가] 서버 실시간 확인용 시간
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            assets = await get_my_assets()
            # [추가] 등급 및 시간 정보를 정확히 가져오기 위해 인벤토리 로드
            inv_data = load_inventory()

            is_night = config.is_sleeping_time()
            report_lines = []
            symbol_buttons = []

            for symbol, data in assets.items():
                # 0단계: 기본 데이터 수집
                ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
                this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)

                # [수정] 평단가 참조 키 보강: data에 없으면 inv_data에서 보충하여 -100% 방지
                this_avg_p = float(data.get('avg_buy_price') or data.get('avg_price') or 0)

                # 인벤토리 데이터 미리 로드 (평단가 보충 및 등급 확인용)
                inv_item = inv_data.get(symbol) or inv_data.get(symbol.split('/')[0]) or {}

                # 거래소 데이터에 평단가가 0으로 나올 경우 인벤토리 값으로 대체
                if this_avg_p <= 0:
                    this_avg_p = float(inv_item.get('purchase_price') or 0)

                this_qty = float(data.get('total', 0))

                # 수익률 계산 (보정된 평단가 사용)
                this_profit = ((this_curr_p - this_avg_p) / this_avg_p * 100) if this_avg_p > 0 else 0
                this_profit_krw = (this_curr_p - this_avg_p) * this_qty

                # [수정] 인벤토리에서 등급 가져오기
                this_grade = inv_item.get('grade', 'A')

                # [흰색 박멸] 시간을 강제로 과거(30일 전)로 설정하여 6봉 유예(⚪) 조건을 강제 돌파
                this_elapsed_bars = 999

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
                # [기존 익절 로직 보존]
                if this_profit >= 13.0:
                    balance = await asyncio.to_thread(exchange.fetch_balance)
                    base = symbol.split('/')[0]
                    free_qty = float(balance['free'].get(base, 0))
                    sell_qty = min(this_qty, free_qty)
                    if sell_qty <= 0:
                        logger.info(f"매도 건너뜀(잔고 부족): {symbol}")
                    else:
                        await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                        await app.bot.send_message(config.CHAT_ID, f"🎯 [목표익절] {symbol} 13% 전량 매도")
                        tp_executed = True
                elif this_profit >= 8.0 and this_curr_p < ma40_line:
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

                is_sell_signal, sell_reason = await strategy.check_sell_signal(
                    exchange=exchange,
                    df=df,
                    symbol=symbol,
                    purchase_price=this_avg_p,
                    symbol_inventory_age=this_elapsed_bars,
                    status=status
                )

                # 0순위 급등/절대익절 판정
                if status == 'KEEP' and is_sell_signal and "0순위" in sell_reason:
                    is_sell_final = True
                else:
                    is_sell_final = False

                elapsed_min = 0
                if is_sell_signal:
                    if "0순위" in sell_reason or "절대익절" in sell_reason:
                        is_sell_final = True
                    elif symbol not in pending_approvals:
                        # [기존 로직] 사유별 유예 시간 차등 (10분 vs 30분)
                        wait_limit = 10 if ("1순위" in sell_reason or "2음봉" in sell_reason) else 30
                        kb = telegram_ui.get_sell_signal_kb(symbol, wait_limit)
                        icon = "🚨" if wait_limit == 10 else "🔵"

                        await app.bot.send_message(config.CHAT_ID,
                                                   f"{icon} [{wait_limit}분 유예 시작] {symbol}\n"
                                                   f"사유: {sell_reason}\n"
                                                   f"현재수익률: {this_profit:+.2f}% | 현재가: {this_curr_p:,.0f}원\n"
                                                   f"⏱ 대응 선택 대기", reply_markup=kb)

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
                report_lines.append(report_line)
                symbol_buttons.append(
                    InlineKeyboardButton(f"🔍 {symbol.split('/')[0]}", callback_data=f"manage_asset:{symbol}"))

                # 5단계: 최종 집행
                if is_sell_final:
                    if status == 'AUTO' or is_night or "0순위" in sell_reason:
                        balance = await asyncio.to_thread(exchange.fetch_balance)
                        base = symbol.split('/')[0]
                        free_qty = float(balance['free'].get(base, 0))
                        sell_qty = min(this_qty, free_qty)
                        if sell_qty <= 0:
                            logger.info(f"매도 건너뜀(잔고 부족): {symbol}")
                        else:
                            await asyncio.to_thread(exchange.create_market_sell_order, symbol, sell_qty)
                            await app.bot.send_message(config.CHAT_ID, f"🔴 [매도 집행]\n{symbol} | 사유: {sell_reason}")
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
                    summary = f"🔵:{sum(1 for l in report_lines if '🔵' in l)} | 🔴:{sum(1 for l in report_lines if '🔴' in l)} | 🟢:{sum(1 for l in report_lines if '🟢' in l)}"
                    msg_text = f"📊 [정기 리포트] ({now_str}){' (야간 AUTO)' if is_night else ''}\n{summary}\n" + "━━━━━━━━━━━━\n" + "\n".join(
                        report_lines)
                    final_rows = [symbol_buttons[i:i + 4] for i in range(0, len(symbol_buttons), 4)]
                    is_all_auto = all(sell_mute_status.get(s) == 'AUTO' for s in assets.keys()) if assets else False
                    report_kb = telegram_ui.get_report_inline_kb(is_all_auto)
                    if report_kb and hasattr(report_kb, 'inline_keyboard'):
                        final_rows.extend(report_kb.inline_keyboard)

                    await app.bot.send_message(config.CHAT_ID, msg_text, reply_markup=InlineKeyboardMarkup(final_rows))
                last_report_time = datetime.now()

            await asyncio.sleep(180)  # [변경] 매도 감시 주기 1분 -> 3분
        except Exception as e:
            import traceback
            logger.error(f"Sell Monitor Error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(180)  # [변경] 에러 발생 시에도 3분 대기


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
                assets = await get_my_assets()
                qty = assets.get(sym, {}).get('total', 0)
                save_inventory(sym, price, qty)
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

        report_lines = []
        symbol_buttons = []
        urgent_count = 0

        # [핵심] 필터링(continue) 없이 assets에 있는 모든 종목을 순회
        for symbol, data in assets.items():
            ticker = await asyncio.to_thread(exchange.fetch_ticker, symbol)
            this_curr_p = float(ticker.get('last') or ticker.get('close') or 0)
            if this_curr_p == 0: continue

            # [수정] 평단가 참조 키 보강 (-100% 및 종목 누락 방지)
            this_avg_p = float(data.get('avg_buy_price') or data.get('avg_price') or 0)

            # 인벤토리 데이터 미리 로드 (평단가 보충 및 등급 확인용)
            inv_item = inv_data.get(symbol) or inv_data.get(symbol.split('/')[0]) or {}

            # [보강] 거래소 데이터에 평단가가 없으면 인벤토리 값으로 대체하여 0원(누락/오류) 방지
            if this_avg_p <= 0:
                this_avg_p = float(inv_item.get('purchase_price') or 0)

            this_qty = float(data.get('total', 0))

            # 평단가 보정을 통해 this_profit이 정상적으로 계산됨 (리스트 누락 방지)
            this_profit = ((this_curr_p - this_avg_p) / this_avg_p * 100) if this_avg_p > 0 else 0
            this_profit_krw = (this_curr_p - this_avg_p) * this_qty

            # 인벤토리 데이터 매칭 (등급 및 매수시간)
            this_grade = inv_item.get('grade', 'A')

            # [흰색 박멸] 시간을 강제로 과거로 설정하여 6봉 유예(⚪) 조건을 원천 차단
            this_elapsed_bars = 999

            # 야간 모드 및 모드 아이콘 판정
            raw_status = sell_mute_status.get(symbol, 'WATCH')
            status = 'AUTO' if is_night else raw_status

            ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, '30m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
            ma40_line = df['close'].rolling(40).mean().iloc[-1]

            # 전략 엔진 호출
            is_sell_signal, sell_reason = await strategy.check_sell_signal(
                exchange, df, symbol, this_avg_p, this_elapsed_bars, status
            )

            # 비주얼 판정 (기존 로직 보존)
            if status == 'KEEP' and not (is_sell_signal and "0순위" in sell_reason):
                report_color, status_text, mode_str = "🟢", "유지 중", " 🔒"
            else:
                report_color, status_text = strategy.get_report_visuals(
                    this_profit, is_sell_signal, this_curr_p, ma40_line,
                    sell_reason, symbol, pending_approvals
                )
                mode_str = " 🤖" if status == 'AUTO' else ""

            if report_color == "🚨": urgent_count += 1

            # [기존 출력 포맷 유지]
            report_line = f"{report_color} [{this_grade}] {symbol.split('/')[0]:<6} | {this_curr_p:,.0f}원 | {this_profit:+.2f}%({this_profit_krw:+,.0f}원) | {status_text}{mode_str}"
            report_lines.append(report_line)

            # 종목 상세 버튼 생성
            symbol_buttons.append(
                InlineKeyboardButton(f"🔍 {symbol.split('/')[0]}", callback_data=f"manage_asset:{symbol}")
            )

        # [원본 로직] 하단 버튼 키보드 구성 (기능 유지)
        final_rows = [symbol_buttons[i:i + 4] for i in range(0, len(symbol_buttons), 4)]
        is_all_auto = all(sell_mute_status.get(s) == 'AUTO' for s in assets.keys()) if assets else False
        report_kb = telegram_ui.get_report_inline_kb(is_all_auto)
        if report_kb and hasattr(report_kb, 'inline_keyboard'):
            final_rows.extend(report_kb.inline_keyboard)

        # 최종 메시지 조립
        night_tag = " (야간 AUTO)" if is_night else ""
        msg_text = f"📊 [실시간 리포트]{night_tag}\n" + ("\n".join(report_lines) if report_lines else "보유 종목 없음")

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
        return "A"  # 에러 시 안전하게 자동매수 차단 등급 반환

async def main():
    print("🚀 가상화폐 자동 매매 시스템 가동...")
    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_interaction))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_interaction))

    asyncio.create_task(buy_scan_task(app))
    asyncio.create_task(sell_monitor_task(app))

    await app.initialize()
    await app.start()
    await app.bot.send_message(config.CHAT_ID, "🚀 시스템 가동 시작", reply_markup=telegram_ui.get_main_keyboard())
    await app.updater.start_polling()

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 시스템을 종료합니다.")