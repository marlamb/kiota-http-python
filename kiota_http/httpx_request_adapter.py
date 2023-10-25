"""HTTPX client request adapter."""
import re
from collections.abc import AsyncIterable, Iterable
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar, Union
from urllib import parse
from urllib.parse import unquote

import httpx
from kiota_abstractions.api_client_builder import (
    enable_backing_store_for_parse_node_factory,
    enable_backing_store_for_serialization_writer_factory,
)
from kiota_abstractions.api_error import APIError
from kiota_abstractions.authentication import AuthenticationProvider
from kiota_abstractions.request_adapter import RequestAdapter
from kiota_abstractions.request_information import RequestInformation
from kiota_abstractions.serialization import (
    Parsable,
    ParsableFactory,
    ParseNode,
    ParseNodeFactory,
    ParseNodeFactoryRegistry,
    SerializationWriterFactory,
    SerializationWriterFactoryRegistry,
)
from kiota_abstractions.store import BackingStoreFactory, BackingStoreFactorySingleton
from opentelemetry import trace
from opentelemetry.semconv.trace import SpanAttributes

from kiota_http._exceptions import BackingStoreError, DeserializationError, RequestError
from kiota_http.middleware.parameters_name_decoding_handler import ParametersNameDecodingHandler

from ._version import VERSION
from .kiota_client_factory import KiotaClientFactory
from .middleware.options import ParametersNameDecodingHandlerOption, ResponseHandlerOption
from .observability_options import ObservabilityOptions

ResponseType = Union[str, int, float, bool, datetime, bytes]
ModelType = TypeVar("ModelType", bound=Parsable)

AUTHENTICATE_CHALLENGED_EVENT_KEY = "com.microsoft.kiota.authenticate_challenge_received"
RESPONSE_HANDLER_EVENT_INVOKED_KEY = "response_handler_invoked"
ERROR_MAPPING_FOUND_KEY = "com.microsoft.kiota.error.mapping_found"
ERROR_BODY_FOUND_KEY = "com.microsoft.kiota.error.body_found"
DESERIALIZED_MODEL_NAME_KEY = "com.microsoft.kiota.response.type"
REQUEST_IS_NULL = RequestError("Request info cannot be null")

tracer = trace.get_tracer(ObservabilityOptions.get_tracer_instrumentation_name(), VERSION)


class HttpxRequestAdapter(RequestAdapter, Generic[ModelType]):
    CLAIMS_KEY = "claims"
    BEARER_AUTHENTICATION_SCHEME = "Bearer"
    RESPONSE_AUTH_HEADER = "WWW-Authenticate"

    def __init__(
        self,
        authentication_provider: AuthenticationProvider,
        parse_node_factory: ParseNodeFactory = ParseNodeFactoryRegistry(),
        serialization_writer_factory:
        SerializationWriterFactory = SerializationWriterFactoryRegistry(),
        http_client: httpx.AsyncClient = KiotaClientFactory.create_with_default_middleware(),
        observability_options=ObservabilityOptions(),
    ) -> None:
        if not authentication_provider:
            raise TypeError("Authentication provider cannot be null")
        self._authentication_provider = authentication_provider
        if not parse_node_factory:
            raise TypeError("Parse node factory cannot be null")
        self._parse_node_factory = parse_node_factory
        if not serialization_writer_factory:
            raise TypeError("Serialization writer factory cannot be null")
        self._serialization_writer_factory = serialization_writer_factory
        if not http_client:
            raise TypeError("Http Client cannot be null")
        if not observability_options:
            observability_options = ObservabilityOptions()

        self._http_client = http_client
        self._base_url: str = ""
        self.observability_options = observability_options

    @property
    def base_url(self) -> str:
        """Gets the base url for every request

        Returns:
            str: The base url
        """
        return self._base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        """Sets the base url for every request

        Args:
            value (str): The new base url
        """
        if value:
            self._base_url = value

    def get_serialization_writer_factory(self) -> SerializationWriterFactory:
        """Gets the serialization writer factory currently in use for the HTTP core service.
        Returns:
            SerializationWriterFactory: the serialization writer factory currently in use for the
            HTTP core service.
        """
        return self._serialization_writer_factory

    def get_response_content_type(self, response: httpx.Response) -> Optional[str]:
        header = response.headers.get("content-type")
        if not header:
            return None
        segments = header.lower().split(";")
        if not segments:
            return None
        return segments[0]

    def start_tracing_span(self, request_info: RequestInformation, method: str) -> trace.Span:
        """Creates an Opentelemetry tracer and starts the parent span.

        Args:
            request_info(RequestInformation): the request object.
            method(str): name of the invoker.

        Returns:
            The parent span.
        """
        uri_template = (request_info.url_template if request_info.url_template else "UNKNOWN")
        decoded_uri_template = unquote(uri_template)
        parent_span_name = f"{method} - {decoded_uri_template}"

        span = tracer.start_span(parent_span_name)
        return span

    def _start_local_tracing_span(self, name: str, parent_span: trace.Span) -> trace.Span:
        """Helper function to start a span locally with the parent context."""
        _context = trace.set_span_in_context(parent_span)
        span = tracer.start_span(name, context=_context)
        return span

    async def send_async(
        self,
        request_info: RequestInformation,
        parsable_factory: ParsableFactory,
        error_map: Dict[str, ParsableFactory],
    ) -> Optional[ModelType]:
        """Excutes the HTTP request specified by the given RequestInformation and returns the
        deserialized response model.
        Args:
            request_info (RequestInformation): the request info to execute.
            parsable_factory (ParsableFactory): the class of the response model
            to deserialize the response into.
            error_map (Dict[str, ParsableFactory]): the error dict to use in
            case of a failed request.

        Returns:
            ModelType: the deserialized response model.
        """
        parent_span = self.start_tracing_span(request_info, "send_async")
        try:
            if not request_info:
                parent_span.record_exception(REQUEST_IS_NULL)
                raise REQUEST_IS_NULL

            response = await self.get_http_response_message(request_info, parent_span)

            response_handler = self.get_response_handler(request_info)
            if response_handler:
                parent_span.add_event(RESPONSE_HANDLER_EVENT_INVOKED_KEY)
                return await response_handler.handle_response_async(response, error_map)

            await self.throw_failed_responses(response, error_map, parent_span, parent_span)
            if self._should_return_none(response):
                return None
            root_node = await self.get_root_parse_node(response, parent_span, parent_span)
            if root_node is None:
                return None
            _deserialized_span = self._start_local_tracing_span("get_object_value", parent_span)
            value = root_node.get_object_value(parsable_factory)
            parent_span.set_attribute(DESERIALIZED_MODEL_NAME_KEY, value.__class__.__name__)
            _deserialized_span.end()
            return value
        finally:
            parent_span.end()

    async def send_collection_async(
        self,
        request_info: RequestInformation,
        parsable_factory: ParsableFactory,
        error_map: Dict[str, ParsableFactory],
    ) -> Optional[List[ModelType]]:
        """Excutes the HTTP request specified by the given RequestInformation and returns the
        deserialized response model collection.
        Args:
            request_info (RequestInformation): the request info to execute.
            parsable_factory (ParsableFactory): the class of the response model
            to deserialize the response into.
            error_map (Dict[str, ParsableFactory]): the error dict to use in
            case of a failed request.

        Returns:
            ModelType: the deserialized response model collection.
        """
        parent_span = self.start_tracing_span(request_info, "send_collection_async")
        try:
            if not request_info:
                parent_span.record_exception(REQUEST_IS_NULL)
                raise REQUEST_IS_NULL
            response = await self.get_http_response_message(request_info, parent_span)

            response_handler = self.get_response_handler(request_info)
            if response_handler:
                parent_span.add_event(RESPONSE_HANDLER_EVENT_INVOKED_KEY)
                return await response_handler.handle_response_async(response, error_map)

            await self.throw_failed_responses(response, error_map, parent_span, parent_span)
            if self._should_return_none(response):
                return None

            _deserialized_span = self._start_local_tracing_span(
                "get_collection_of_object_values", parent_span
            )
            root_node = await self.get_root_parse_node(response, parent_span, parent_span)
            result = root_node.get_collection_of_object_values(parsable_factory)
            parent_span.set_attribute(DESERIALIZED_MODEL_NAME_KEY, result.__class__.__name__)
            _deserialized_span.end()
            return result
        finally:
            parent_span.end()

    async def send_collection_of_primitive_async(
        self,
        request_info: RequestInformation,
        response_type: ResponseType,
        error_map: Dict[str, ParsableFactory],
    ) -> Optional[List[ResponseType]]:
        """Excutes the HTTP request specified by the given RequestInformation and returns the
        deserialized response model collection.
        Args:
            request_info (RequestInformation): the request info to execute.
            response_type (ResponseType): the class of the response model
            to deserialize the response into.
            error_map (Dict[str, ParsableFactory]): the error dict to use in
            case of a failed request.

        Returns:
            Optional[List[ModelType]]: he deserialized response model collection.
        """
        parent_span = self.start_tracing_span(request_info, "send_collection_of_primitive_async")
        try:
            if not request_info:
                parent_span.record_exception(REQUEST_IS_NULL)
                raise REQUEST_IS_NULL

            response = await self.get_http_response_message(request_info, parent_span)

            response_handler = self.get_response_handler(request_info)
            if response_handler:
                parent_span.add_event(RESPONSE_HANDLER_EVENT_INVOKED_KEY)
                return await response_handler.handle_response_async(response, error_map)

            await self.throw_failed_responses(response, error_map, parent_span, parent_span)
            if self._should_return_none(response):
                return None
            root_node = await self.get_root_parse_node(response, parent_span, parent_span)

            _deserialized_span = self._start_local_tracing_span(
                "get_collection_of_primitive_values", parent_span
            )
            root_node = await self.get_root_parse_node(response, parent_span, parent_span)
            values = root_node.get_collection_of_primitive_values(response_type)
            parent_span.set_attribute(DESERIALIZED_MODEL_NAME_KEY, values.__class__.__name__)
            _deserialized_span.end()
            return values
        finally:
            parent_span.end()

    async def send_primitive_async(
        self,
        request_info: RequestInformation,
        response_type: ResponseType,
        error_map: Dict[str, ParsableFactory],
    ) -> Optional[ResponseType]:
        """Excutes the HTTP request specified by the given RequestInformation and returns the
        deserialized primitive response model.
        Args:
            request_info (RequestInformation): the request info to execute.
            response_type (ResponseType): the class of the response model to deserialize the
            response into.
            error_map (Dict[str, ParsableFactory]): the error dict to use in case
            of a failed request.

        Returns:
            ResponseType: the deserialized primitive response model.
        """
        parent_span = self.start_tracing_span(request_info, "send_primitive_async")
        try:
            if not request_info:
                parent_span.record_exception(REQUEST_IS_NULL)
                raise REQUEST_IS_NULL

            response = await self.get_http_response_message(request_info, parent_span)

            response_handler = self.get_response_handler(request_info)
            if response_handler:
                parent_span.add_event(RESPONSE_HANDLER_EVENT_INVOKED_KEY)
                return await response_handler.handle_response_async(response, error_map)

            await self.throw_failed_responses(response, error_map, parent_span, parent_span)
            if self._should_return_none(response):
                return None
            if response_type == "bytes":
                return response.content
            _deserialized_span = self._start_local_tracing_span("get_root_parse_node", parent_span)
            root_node = await self.get_root_parse_node(response, parent_span, parent_span)
            value = None
            if response_type == "str":
                value = root_node.get_str_value()
            if response_type == "int":
                value = root_node.get_int_value()
            if response_type == "float":
                value = root_node.get_float_value()
            if response_type == "bool":
                value = root_node.get_bool_value()
            if response_type == "datetime":
                value = root_node.get_datetime_value()
            if value is not None:
                parent_span.set_attribute(DESERIALIZED_MODEL_NAME_KEY, value.__class__.__name__)
                _deserialized_span.end()
                return value

            exc = TypeError(f"Unable to deserialize type: {response_type!r}")
            parent_span.record_exception(exc)
            _deserialized_span.end()
            raise exc

        finally:
            parent_span.end()

    async def send_no_response_content_async(
        self, request_info: RequestInformation, error_map: Dict[str, ParsableFactory]
    ) -> None:
        """Excutes the HTTP request specified by the given RequestInformation and returns the
        deserialized primitive response model.
        Args:
            request_info (RequestInformation):the request info to execute.
            error_map (Dict[str, ParsableFactory]): the error dict to use in case
            of a failed request.
        """
        parent_span = self.start_tracing_span(request_info, "send_no_response_content_async")
        try:
            if not request_info:
                parent_span.record_exception(REQUEST_IS_NULL)
                raise REQUEST_IS_NULL

            response = await self.get_http_response_message(request_info, parent_span)

            response_handler = self.get_response_handler(request_info)
            if response_handler:
                parent_span.add_event(RESPONSE_HANDLER_EVENT_INVOKED_KEY)
                return await response_handler.handle_response_async(response, error_map)

            await self.throw_failed_responses(response, error_map, parent_span, parent_span)
        finally:
            parent_span.end()

    def enable_backing_store(self, backing_store_factory: Optional[BackingStoreFactory]) -> None:
        """Enables the backing store proxies for the SerializationWriters and ParseNodes in use.
        Args:
            backing_store_factory (Optional[BackingStoreFactory]): the backing store factory to use.
        """
        self._parse_node_factory = enable_backing_store_for_parse_node_factory(
            self._parse_node_factory
        )
        self._serialization_writer_factory = (
            enable_backing_store_for_serialization_writer_factory(
                self._serialization_writer_factory
            )
        )
        if not any([self._serialization_writer_factory, self._parse_node_factory]):
            raise BackingStoreError("Unable to enable backing store")
        if backing_store_factory:
            BackingStoreFactorySingleton(backing_store_factory=backing_store_factory)

    async def get_root_parse_node(
        self,
        response: httpx.Response,
        parent_span: trace.Span,
        attribute_span: trace.Span,
    ) -> ParseNode:
        span = self._start_local_tracing_span("get_root_parse_node", parent_span)

        try:
            payload = response.content
            response_content_type = self.get_response_content_type(response)
            if not response_content_type:
                raise DeserializationError("No response content type found for deserialization")
            return self._parse_node_factory.get_root_parse_node(response_content_type, payload)
        finally:
            span.end()

    def _should_return_none(self, response: httpx.Response) -> bool:
        return response.status_code == 204

    async def throw_failed_responses(
        self,
        response: httpx.Response,
        error_map: Dict[str, ParsableFactory],
        parent_span: trace.Span,
        attribute_span: trace.Span,
    ) -> None:
        if response.is_success:
            return
        try:
            attribute_span.set_status(trace.StatusCode.ERROR)

            _throw_failed_resp_span = self._start_local_tracing_span(
                "throw_failed_responses", parent_span
            )

            response_status_code = response.status_code
            response_status_code_str = str(response_status_code)
            response_headers = response.headers

            _throw_failed_resp_span.set_attribute("status", response_status_code)
            _throw_failed_resp_span.set_attribute(ERROR_MAPPING_FOUND_KEY, bool(error_map))
            if not error_map:
                exc = APIError(
                    "The server returned an unexpected status code and no error class is registered"
                    f" for this code {response_status_code}",
                    response_status_code,
                    response_headers,
                )
                # set this or ignore as description in set_status?
                _throw_failed_resp_span.set_attribute("status_message", "received_error_response")
                _throw_failed_resp_span.set_status(trace.StatusCode.ERROR, str(exc))
                attribute_span.record_exception(exc)
                raise exc

            if (response_status_code_str not in error_map) and (
                (400 <= response_status_code < 500 and "4XX" not in error_map) or
                (500 <= response_status_code < 600 and "5XX" not in error_map)
            ):
                exc = APIError(
                    "The server returned an unexpected status code and no error class is registered"
                    f" for this code {response_status_code}",
                    response_status_code,
                    response_headers,
                )
                attribute_span.record_exception(exc)
                raise exc
            _throw_failed_resp_span.set_attribute("status_message", "received_error_response")

            error_class = None
            if response_status_code_str in error_map:
                error_class = error_map[response_status_code_str]
            elif 400 <= response_status_code < 500:
                error_class = error_map["4XX"]
            elif 500 <= response_status_code < 600:
                error_class = error_map["5XX"]

            root_node = await self.get_root_parse_node(
                response, _throw_failed_resp_span, _throw_failed_resp_span
            )
            attribute_span.set_attribute(ERROR_BODY_FOUND_KEY, bool(root_node))

            _get_obj_ctx = trace.set_span_in_context(_throw_failed_resp_span)
            _get_obj_span = tracer.start_span("get_object_value", context=_get_obj_ctx)

            error = root_node.get_object_value(error_class)
            if isinstance(error, APIError):
                error.response_headers = response_headers
                error.response_status_code = response_status_code
                exc = error
            else:
                exc = APIError(
                    f"Unexpected error type: {type(error)}",
                    response_status_code,
                    response_headers,
                )
            _get_obj_span.end()
            raise exc
        finally:
            _throw_failed_resp_span.end()

    async def get_http_response_message(
        self,
        request_info: RequestInformation,
        parent_span: trace.Span,
        claims: str = "",
    ) -> httpx.Response:
        _get_http_resp_span = self._start_local_tracing_span(
            "get_http_response_message", parent_span
        )

        self.set_base_url_for_request_information(request_info)

        additional_authentication_context = {}
        if claims:
            additional_authentication_context[self.CLAIMS_KEY] = claims

        await self._authentication_provider.authenticate_request(
            request_info, additional_authentication_context
        )

        request = self.get_request_from_request_information(
            request_info, _get_http_resp_span, parent_span
        )
        resp = await self._http_client.send(request)
        parent_span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, resp.status_code)
        if http_version := resp.http_version:
            parent_span.set_attribute(SpanAttributes.HTTP_FLAVOR, http_version)

        if content_length := resp.headers.get("Content-Length", None):
            parent_span.set_attribute(SpanAttributes.HTTP_RESPONSE_CONTENT_LENGTH, content_length)

        if content_type := resp.headers.get("Content-Type", None):
            parent_span.set_attribute("http.response_content_type", content_type)
        _get_http_resp_span.end()
        return await self.retry_cae_response_if_required(resp, request_info, claims)

    async def retry_cae_response_if_required(
        self, resp: httpx.Response, request_info: RequestInformation, claims: str
    ) -> httpx.Response:
        parent_span = self.start_tracing_span(request_info, "retry_cae_response_if_required")
        if (
            resp.status_code == 401
            and not claims  # previous claims exist. Means request has already been retried
            and resp.headers.get(self.RESPONSE_AUTH_HEADER)
        ):
            auth_header_value = resp.headers.get(self.RESPONSE_AUTH_HEADER)
            if auth_header_value.casefold().startswith(
                self.BEARER_AUTHENTICATION_SCHEME.casefold()
            ):
                claims_match = re.search('claims="(.+)"', auth_header_value)
                if not claims_match:
                    raise ValueError("Unable to parse claims from response")
                response_claims = claims_match.group().split('="')[1]
                parent_span.add_event(AUTHENTICATE_CHALLENGED_EVENT_KEY)
                parent_span.set_attribute("http.retry_count", 1)
                return await self.get_http_response_message(
                    request_info, parent_span, response_claims
                )
            return resp
        return resp

    def get_response_handler(self, request_info: RequestInformation) -> Any:
        response_handler_option = request_info.request_options.get(ResponseHandlerOption.get_key())
        if response_handler_option:
            return response_handler_option.response_handler
        return None

    def set_base_url_for_request_information(self, request_info: RequestInformation) -> None:
        request_info.path_parameters["baseurl"] = self.base_url

    def get_request_from_request_information(
        self,
        request_info: RequestInformation,
        parent_span: trace.Span,
        attribute_span: trace.Span,
    ) -> httpx.Request:
        _get_request_span = self._start_local_tracing_span(
            "get_request_from_request_information", parent_span
        )
        url = parse.urlparse(request_info.url)
        otel_attributes = {
            SpanAttributes.HTTP_METHOD: request_info.http_method,
            "http.port": url.port,
            SpanAttributes.HTTP_HOST: url.hostname,
            SpanAttributes.HTTP_SCHEME: url.scheme,
            "http.uri_template": request_info.url_template,
        }

        if self.observability_options.include_euii_attributes:
            otel_attributes.update({"http.uri": url.geturl()})

        request = self._http_client.build_request(
            method=request_info.http_method.value,
            url=request_info.url,
            headers=request_info.request_headers,
            content=request_info.content,
        )
        request_options = {
            self.observability_options.get_key(): self.observability_options,
            "parent_span": parent_span,
            **request_info.request_options,
        }
        setattr(request, "options", request_options)

        if content_length := request.headers.get("Content-Length", None):
            otel_attributes.update({SpanAttributes.HTTP_REQUEST_CONTENT_LENGTH: content_length})

        if content_type := request.headers.get("Content-Type", None):
            otel_attributes.update({"http.request_content_type": content_type})
        attribute_span.set_attributes(otel_attributes)
        _get_request_span.set_attributes(otel_attributes)
        _get_request_span.end()

        return request

    async def convert_to_native_async(self, request_info: RequestInformation) -> httpx.Request:
        parent_span = self.start_tracing_span(request_info, "convert_to_native_async")
        try:
            if request_info is None:
                exc = ValueError("request information must be provided")
                parent_span.record_exception(exc)
                raise exc

            await self._authentication_provider.authenticate_request(request_info)

            request = self.get_request_from_request_information(
                request_info, parent_span, parent_span
            )
            return request
        finally:
            parent_span.end()
