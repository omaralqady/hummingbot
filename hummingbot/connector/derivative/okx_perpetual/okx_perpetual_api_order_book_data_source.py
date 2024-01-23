import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import pandas as pd

from hummingbot.connector.derivative.okx_perpetual import (
    okx_perpetual_constants as CONSTANTS,
    okx_perpetual_web_utils as web_utils,
)
from hummingbot.core.data_type.common import TradeType
from hummingbot.core.data_type.funding_info import FundingInfo, FundingInfoUpdate
from hummingbot.core.data_type.order_book import OrderBookMessage
from hummingbot.core.data_type.order_book_message import OrderBookMessageType
from hummingbot.core.data_type.perpetual_api_order_book_data_source import PerpetualAPIOrderBookDataSource
from hummingbot.core.utils.tracking_nonce import NonceCreator
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant

if TYPE_CHECKING:
    from hummingbot.connector.derivative.okx_perpetual.okx_perpetual_derivative import OKXPerpetualDerivative


class OKXPerpetualAPIOrderBookDataSource(PerpetualAPIOrderBookDataSource):
    def __init__(
        self,
        trading_pairs: List[str],
        connector: OKXPerpetualDerivative,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN
    ):
        super().__init__(trading_pairs)
        self._connector = connector
        self._api_factory = api_factory
        self._domain = domain
        self._nonce_provider = NonceCreator.for_microseconds()

    async def get_last_traded_prices(self, trading_pairs: List[str], domain: Optional[str] = None) -> Dict[str, float]:
        return await self._connector.get_last_traded_prices()

    async def get_funding_info(self, trading_pair: str) -> FundingInfo:
        funding_info_response = await self._request_complete_funding_info(trading_pair)
        index_price = funding_info_response[0]["data"][0]
        mark_price = funding_info_response[1]["data"][0]
        funding_data = funding_info_response[2]["data"][0]
        funding_info = FundingInfo(
            trading_pair=trading_pair,
            index_price=Decimal(str(index_price["idxPx"])),
            mark_price=Decimal(str(mark_price["markPx"])),
            next_funding_utc_timestamp=int(funding_data["nextFundingTime"]),
            rate=Decimal(str(funding_data["nextFundingRate"])),
        )
        return funding_info

    async def _request_complete_funding_info(self, trading_pair: str):
        tasks = []
        rest_assistant = await self._api_factory.get_rest_assistant()
        inst_id = await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)

        params_index_price = {
            "instId": inst_id
        }
        endpoint_index_price = CONSTANTS.INDEX_TICKERS_PATH_URL
        url_index_price = web_utils.get_rest_url_for_endpoint(endpoint=endpoint_index_price, domain=self._domain)
        limit_id_index_price = web_utils.get_rest_api_limit_id_for_endpoint(endpoint_index_price)
        tasks.append(rest_assistant.execute_request(
            url=url_index_price,
            throttler_limit_id=limit_id_index_price,
            params=params_index_price,
            method=RESTMethod.GET,
        ))

        params_mark_price = {
            "instId": inst_id,
            "instType": "SWAP",
        }
        endpoint_mark_price = CONSTANTS.MARK_PRICE_PATH_URL
        url_predicted = web_utils.get_rest_url_for_endpoint(endpoint=endpoint_mark_price, domain=self._domain)
        limit_id_mark_price = web_utils.get_rest_api_limit_id_for_endpoint(endpoint_mark_price, trading_pair)
        tasks.append(rest_assistant.execute_request(
            url=url_predicted,
            throttler_limit_id=limit_id_mark_price,
            params=params_mark_price,
            method=RESTMethod.GET,
            is_auth_required=True
        ))

        params_funding_data = {
            "instId": inst_id
        }
        endpoint_funding_data = CONSTANTS.FUNDING_RATE_INFO_PATH_URL
        url_funding_data = web_utils.get_rest_url_for_endpoint(endpoint=endpoint_funding_data, domain=self._domain)
        limit_id_funding_data = web_utils.get_rest_api_limit_id_for_endpoint(endpoint_funding_data)
        tasks.append(rest_assistant.execute_request(
            url=url_funding_data,
            throttler_limit_id=limit_id_funding_data,
            params=params_funding_data,
            method=RESTMethod.GET,
        ))

        responses = await asyncio.gather(*tasks)
        return responses

    async def listen_for_subscriptions(self):
        """
        Subscribe to all required events and start the listening cycle.
        """
        tasks_future = None
        try:
            tasks = [self._listen_for_subscriptions_on_url(url=web_utils.wss_linear_public_url(self._domain),
                                                           trading_pairs=self._trading_pairs)]
            if tasks:
                tasks_future = asyncio.gather(*tasks)
                await tasks_future

        except asyncio.CancelledError:
            tasks_future and tasks_future.cancel()
            raise

    async def _listen_for_subscriptions_on_url(self, url: str, trading_pairs: List[str]):
        """
        Subscribe to all required events and start the listening cycle.
        :param url: the wss url to connect to
        :param trading_pairs: the trading pairs for which the function should listen events
        """

        ws: Optional[WSAssistant] = None
        while True:
            try:
                ws = await self._get_connected_websocket_assistant(url)
                await self._subscribe_to_channels(ws, trading_pairs)
                await self._process_websocket_messages(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().exception(
                    f"Unexpected error occurred when listening to order book streams {url}. Retrying in 5 seconds..."
                )
                await self._sleep(5.0)
            finally:
                ws and await ws.disconnect()

    async def _get_connected_websocket_assistant(self, ws_url: str) -> WSAssistant:
        ws: WSAssistant = await self._api_factory.get_ws_assistant()
        await ws.connect(
            ws_url=ws_url, message_timeout=CONSTANTS.SECONDS_TO_WAIT_TO_RECEIVE_MESSAGE
        )
        return ws

    async def _subscribe_to_channels(self, ws: WSAssistant, trading_pairs: List[str]):
        try:
            ex_trading_pairs = [
                await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
                for trading_pair in trading_pairs
            ]

            trades_args = [
                {
                    "channel": CONSTANTS.WS_TRADES_ALL_CHANNEL,
                    "instId": ex_trading_pair
                } for ex_trading_pair in ex_trading_pairs
            ]
            trades_payload = {
                "op": "subscribe",
                "args": trades_args,
            }
            subscribe_trades_request = WSJSONRequest(payload=trades_payload)

            order_book_args = [
                {
                    "channel": CONSTANTS.WS_ORDER_BOOK_400_DEPTH_100_MS_EVENTS_CHANNEL,
                    "instId": ex_trading_pair
                } for ex_trading_pair in ex_trading_pairs
            ]
            order_book_payload = {
                "op": "subscribe",
                "args": order_book_args,
            }
            subscribe_orderbook_request = WSJSONRequest(payload=order_book_payload)

            instruments_args = [
                {
                    "channel": CONSTANTS.WS_INSTRUMENTS_INFO_CHANNEL,
                    "instId": ex_trading_pair
                } for ex_trading_pair in ex_trading_pairs
            ]
            instruments_payload = {
                "op": "subscribe",
                "args": instruments_args,
            }
            subscribe_instruments_request = WSJSONRequest(payload=instruments_payload)

            # TODO: Add 3 rps Rate Limit / 480 prh Rate Limit?
            await ws.send(subscribe_trades_request)
            await ws.send(subscribe_orderbook_request)
            await ws.send(subscribe_instruments_request)
            self.logger().info("Subscribed to public order book, trade and funding info channels...")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger().exception("Unexpected error occurred subscribing to order book trading and delta streams...")
            raise

    async def _process_websocket_messages(self, websocket_assistant: WSAssistant):
        while True:
            try:
                await super()._process_websocket_messages(websocket_assistant=websocket_assistant)
            except asyncio.TimeoutError:
                ping_request = WSJSONRequest(payload="ping")
                await websocket_assistant.send(ping_request)

    def _channel_originating_message(self, event_message: Dict[str, Any]) -> str:
        channel = ""
        if "success" not in event_message:
            event_channel = event_message["topic"]
            event_channel = ".".join(event_channel.split(".")[:-1])
            if event_channel == CONSTANTS.WS_TRADES_CHANNEL:
                channel = self._trade_messages_queue_key
            elif event_channel == CONSTANTS.WS_ORDER_BOOK_400_DEPTH_100_MS_EVENTS_CHANNEL:
                channel = self._diff_messages_queue_key
            elif event_channel == CONSTANTS.WS_INSTRUMENTS_INFO_CHANNEL:
                channel = self._funding_info_messages_queue_key
        return channel

    # TODO: Check if diff message needs to update certain parts of the orderbook or just stream
    async def _parse_order_book_diff_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        event_type = raw_message.get("action", None)
        if event_type == "update":
            symbol = raw_message["arg"]["instId"]
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol)
            timestamp_us = int(raw_message["data"][0]["ts"])
            update_id = self._nonce_provider.get_tracking_nonce(timestamp=timestamp_us * 1e-6)
            diffs_data = raw_message["data"][0]
            bids, asks = self._get_bids_and_asks_from_ws_msg_data(diffs_data)
            order_book_message_content = {
                "trading_pair": trading_pair,
                "update_id": update_id,
                "bids": bids,
                "asks": asks,
            }
            diff_message = OrderBookMessage(
                message_type=OrderBookMessageType.DIFF,
                content=order_book_message_content,
                timestamp=timestamp_us * 1e-6,
            )
            message_queue.put_nowait(diff_message)

    async def _parse_trade_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        trade_updates = raw_message["data"]
        for trade_data in trade_updates:
            symbol = trade_data["instId"]
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol)
            ts_ms = int(trade_data["ts"])
            trade_type = float(TradeType.BUY.value) if trade_data["side"] == "buy" else float(TradeType.SELL.value)
            message_content = {
                "trade_id": trade_data["tradeId"],
                "trading_pair": trading_pair,
                "trade_type": trade_type,
                "amount": trade_data["sz"],
                "price": trade_data["px"],
            }
            trade_message = OrderBookMessage(
                message_type=OrderBookMessageType.TRADE,
                content=message_content,
                timestamp=ts_ms * 1e-3,
            )
            message_queue.put_nowait(trade_message)

    async def _parse_funding_info_message(self, raw_message: Dict[str, Any], message_queue: asyncio.Queue):
        event_type = raw_message["type"]
        if event_type == "delta":
            symbol = raw_message["topic"].split(".")[-1]
            trading_pair = await self._connector.trading_pair_associated_to_exchange_symbol(symbol)
            entries = raw_message["data"]["update"]
            for entry in entries:
                info_update = FundingInfoUpdate(trading_pair)
                if "index_price" in entry:
                    info_update.index_price = Decimal(str(entry["index_price"]))
                if "mark_price" in entry:
                    info_update.mark_price = Decimal(str(entry["mark_price"]))
                if "next_funding_time" in entry:
                    info_update.next_funding_utc_timestamp = int(
                        pd.Timestamp(str(entry["next_funding_time"]), tz="UTC").timestamp()
                    )
                if "predicted_funding_rate_e6" in entry:
                    info_update.rate = (
                        Decimal(str(entry["predicted_funding_rate_e6"])) * Decimal(1e-6)
                    )
                message_queue.put_nowait(info_update)

    async def _order_book_snapshot(self, trading_pair: str) -> OrderBookMessage:
        snapshot_response = await self._request_order_book_snapshot(trading_pair)
        snapshot_data = snapshot_response["data"][0]
        timestamp = float(snapshot_data["ts"])
        update_id = self._nonce_provider.get_tracking_nonce(timestamp=timestamp)

        bids, asks = self._get_bids_and_asks_from_rest_msg_data(snapshot_data)
        order_book_message_content = {
            "trading_pair": trading_pair,
            "update_id": update_id,
            "bids": bids,
            "asks": asks,
        }
        snapshot_msg: OrderBookMessage = OrderBookMessage(
            message_type=OrderBookMessageType.SNAPSHOT,
            content=order_book_message_content,
            timestamp=timestamp,
        )

        return snapshot_msg

    async def _request_order_book_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        params = {
            "instId": await self._connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair),
            "sz": "100"
        }

        rest_assistant = await self._api_factory.get_rest_assistant()
        endpoint = CONSTANTS.ORDER_BOOK_ENDPOINT
        url = web_utils.get_rest_url_for_endpoint(endpoint=endpoint, domain=self._domain)
        limit_id = web_utils.get_rest_api_limit_id_for_endpoint(endpoint)
        data = await rest_assistant.execute_request(
            url=url,
            throttler_limit_id=limit_id,
            params=params,
            method=RESTMethod.GET,
        )

        return data

    @staticmethod
    def _get_bids_and_asks_from_rest_msg_data(
        snapshot: List[Dict[str, Union[str, int, float]]]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        # asks: ascending, bids: descending
        bids = [tuple(map(float, row[:2])) for row in snapshot['bids']]
        asks = [tuple(map(float, row[:2])) for row in snapshot['asks']]
        return bids, asks

    @staticmethod
    def _get_bids_and_asks_from_ws_msg_data(
        snapshot: Dict[str, List[Dict[str, Union[str, int, float]]]]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        bids = [tuple(map(float, row[:2])) for row in snapshot['bids']]
        asks = [tuple(map(float, row[:2])) for row in snapshot['asks']]
        return bids, asks

    async def _connected_websocket_assistant(self) -> WSAssistant:
        pass  # unused

    async def _subscribe_channels(self, ws: WSAssistant):
        pass  # unused
