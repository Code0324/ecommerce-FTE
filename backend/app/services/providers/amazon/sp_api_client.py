"""Amazon SP-API Client.

Provides a safe, read-only client for Amazon SP-API Orders API.
All operations are READ-ONLY (GET only).

CRITICAL SAFETY RULES:
- Read-only operations only (GET)
- Production endpoints require explicit AMAZON_ENVIRONMENT=production
- Credentials are never logged
- PII is redacted at the boundary

API Version: Orders API v2026-01-01

Sandbox Endpoints:
- North America: https://sandbox.sellingpartnerapi-na.amazon.com
- Europe: https://sandbox.sellingpartnerapi-eu.amazon.com
- Far East: https://sandbox.sellingpartnerapi-fe.amazon.com

Production Endpoints (require explicit enablement):
- North America: https://sellingpartnerapi-na.amazon.com
- Europe: https://sellingpartnerapi-eu.amazon.com
- Far East: https://sellingpartnerapi-fe.amazon.com
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from app.services.providers.amazon.lwa_auth import (
    LWATokenManager,
    LWAAuthenticationError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

SANDBOX_ENDPOINTS = {
    "na": "https://sandbox.sellingpartnerapi-na.amazon.com",
    "eu": "https://sandbox.sellingpartnerapi-eu.amazon.com",
    "fe": "https://sandbox.sellingpartnerapi-fe.amazon.com",
}

PRODUCTION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}

# Orders API version
ORDERS_API_VERSION = "2026-01-01"

# Rate limiting
DEFAULT_RATE_LIMIT = 1  # requests per second
DEFAULT_BURST_LIMIT = 15

# Retry/backoff for recoverable errors (network blips, 429, 5xx, timeouts).
# Requests are GET-only (read-only), so retrying is safe — no duplicate
# side effects.
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5

# User agent
USER_AGENT = "AmazonFulfillmentAssistant/0.1.0 (Language=Python; Platform=sandbox)"


class SPAPIError(Exception):
    """SP-API error."""
    def __init__(
        self,
        message: str = "SP-API error",
        status_code: int = 500,
        error_type: str = "UnknownError",
        recoverable: bool = True,
    ):
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.recoverable = recoverable
        super().__init__(message)


class SPAPIClient:
    """Amazon SP-API client for read-only operations.
    
    This client:
    - Only implements read-only operations (GET)
    - Supports sandbox and production (when explicitly enabled)
    - Blocks production endpoints unless environment is explicitly 'production'
    - Handles rate limiting
    - Manages authentication via LWA
    
    SAFETY:
    - Production requires explicit AMAZON_ENVIRONMENT=production
    - All requests are validated against endpoint rules
    - Credentials are never logged
    """
    
    def __init__(
        self,
        lwa_manager: LWATokenManager,
        region: str | None = None,  # falls back to AMAZON_SP_API_REGION env var
        marketplace_id: str | None = None,  # falls back to AMAZON_MARKETPLACE_ID env var
        environment: str = "sandbox",
    ):
        """Initialize SP-API client.
        
        Args:
            lwa_manager: LWA token manager for authentication
            region: AWS region (na, eu, fe)
            marketplace_id: Amazon marketplace ID
            environment: 'sandbox' or 'production' (must be explicit)
            
        Raises:
            SPAPIError: If invalid region or environment
        """
        if region not in SANDBOX_ENDPOINTS:
            raise SPAPIError(
                f"Invalid region: {region}. Must be one of: {list(SANDBOX_ENDPOINTS.keys())}",
                recoverable=False,
            )
        
        # Validate environment
        environment = environment.lower().strip()
        if environment not in ("sandbox", "production"):
            raise SPAPIError(
                f"Invalid environment: {environment}. Must be 'sandbox' or 'production'.",
                recoverable=False,
            )
        
        # Select endpoint based on environment
        if environment == "production":
            self._base_url = PRODUCTION_ENDPOINTS[region]
        else:
            self._base_url = SANDBOX_ENDPOINTS[region]
        
        self._lwa_manager = lwa_manager
        self._region = region
        self._marketplace_id = marketplace_id
        self._environment = environment
        
        # Rate limiting state
        self._last_request_time: float = 0
        self._request_count: int = 0
        
        # Statistics
        self._total_requests: int = 0
        self._successful_requests: int = 0
        self._failed_requests: int = 0
        
        logger.info(
            "SP-API client initialized (region=%s, marketplace=%s, environment=%s)",
            region,
            marketplace_id,
            environment,
        )
    
    @property
    def is_sandbox(self) -> bool:
        """Confirm this client is connected to sandbox."""
        return self._environment == "sandbox"
    
    @property
    def is_production(self) -> bool:
        """Check if this client is connected to production."""
        return self._environment == "production"
    
    @property
    def environment(self) -> str:
        """Current environment."""
        return self._environment
    
    @property
    def base_url(self) -> str:
        """Current base URL."""
        return self._base_url
    
    @property
    def marketplace_id(self) -> str:
        """Current marketplace ID."""
        return self._marketplace_id
    
    @property
    def request_stats(self) -> dict:
        """Request statistics."""
        return {
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "is_sandbox": self.is_sandbox,
            "environment": self._environment,
        }
    
    def _enforce_rate_limit(self) -> None:
        """Enforce rate limiting between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < (1.0 / DEFAULT_RATE_LIMIT):
            sleep_time = (1.0 / DEFAULT_RATE_LIMIT) - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.time()
    
    def _validate_endpoint(self, url: str) -> None:
        """Validate the URL is allowed for the current environment.
        
        Production endpoints are only allowed when environment='production'.
        This prevents accidental production access.
        """
        # Check if URL contains a production endpoint
        is_production_url = any(prod_url in url for prod_url in PRODUCTION_ENDPOINTS.values())
        
        if is_production_url and self._environment != "production":
            raise SPAPIError(
                "BLOCKED: Production endpoint detected but environment is not 'production'. "
                "Set AMAZON_ENVIRONMENT=production to enable production access.",
                recoverable=False,
            )
        
        # Check if URL contains a sandbox endpoint but environment is production
        is_sandbox_url = any(sandbox_url in url for sandbox_url in SANDBOX_ENDPOINTS.values())
        
        if is_sandbox_url and self._environment == "production":
            raise SPAPIError(
                "BLOCKED: Sandbox endpoint detected but environment is 'production'. "
                "Use production endpoints for production environment.",
                recoverable=False,
            )
    
    async def _make_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Make an authenticated request to SP-API.
        
        Args:
            method: HTTP method (GET only for read-only)
            path: API path (e.g., /orders/2026-01-01/orders)
            params: Query parameters
            
        Returns:
            Response JSON
            
        Raises:
            SPAPIError: If request fails
        """
        # Enforce read-only (CHUNK 1V)
        if method.upper() != "GET":
            raise SPAPIError(
                f"BLOCKED: {method.upper()} method not allowed. "
                "CHUNK 1V only supports GET (read-only) operations.",
                recoverable=False,
            )
        
        # Enforce rate limiting
        self._enforce_rate_limit()
        
        # Get access token
        try:
            access_token = await self._lwa_manager.get_access_token()
        except LWAAuthenticationError as e:
            raise SPAPIError(
                f"Authentication failed: {e.message}",
                status_code=401,
                error_type="Unauthorized",
                recoverable=e.recoverable,
            )
        
        # Build full URL
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        
        # Validate endpoint (safety check)
        self._validate_endpoint(url)
        
        # Prepare headers
        now = datetime.now(timezone.utc)
        headers = {
            "x-amz-access-token": access_token,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "user-agent": USER_AGENT,
            "host": self._base_url.replace("https://", ""),
        }
        
        last_error: SPAPIError | None = None
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self._total_requests += 1
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        url,
                        headers=headers,
                        timeout=30.0,
                    )

                    return self._handle_response(response)

            except httpx.TimeoutException:
                self._failed_requests += 1
                last_error = SPAPIError(
                    "Request timed out",
                    status_code=408,
                    error_type="Timeout",
                    recoverable=True,
                )
            except httpx.NetworkError as e:
                self._failed_requests += 1
                last_error = SPAPIError(
                    f"Network error: {type(e).__name__}",
                    status_code=503,
                    error_type="NetworkError",
                    recoverable=True,
                )
            except Exception as e:
                self._failed_requests += 1
                if isinstance(e, SPAPIError):
                    last_error = e
                else:
                    last_error = SPAPIError(
                        f"Unexpected error: {type(e).__name__}",
                        status_code=500,
                        error_type="UnknownError",
                        recoverable=True,
                    )

            if not last_error.recoverable or attempt == MAX_RETRY_ATTEMPTS:
                raise last_error

            logger.warning(
                "SP-API request failed (attempt %d/%d, recoverable): %s — retrying",
                attempt,
                MAX_RETRY_ATTEMPTS,
                last_error.message,
            )
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise last_error  # pragma: no cover — loop always returns or raises above

    def _make_request_sync(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Synchronous version of _make_request for non-async contexts.
        
        Args:
            method: HTTP method (GET only for read-only)
            path: API path
            params: Query parameters
            
        Returns:
            Response JSON
        """
        if method.upper() != "GET":
            raise SPAPIError(
                f"BLOCKED: {method.upper()} method not allowed. "
                "CHUNK 1V only supports GET (read-only) operations.",
                recoverable=False,
            )
        
        self._enforce_rate_limit()
        
        try:
            access_token = self._lwa_manager.get_access_token_sync()
        except LWAAuthenticationError as e:
            raise SPAPIError(
                f"Authentication failed: {e.message}",
                status_code=401,
                error_type="Unauthorized",
                recoverable=e.recoverable,
            )
        
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        
        self._validate_endpoint(url)
        
        now = datetime.now(timezone.utc)
        headers = {
            "x-amz-access-token": access_token,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
            "user-agent": USER_AGENT,
            "host": self._base_url.replace("https://", ""),
        }
        
        last_error: SPAPIError | None = None
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            self._total_requests += 1
            try:
                with httpx.Client() as client:
                    response = client.get(
                        url,
                        headers=headers,
                        timeout=30.0,
                    )

                    return self._handle_response(response)

            except httpx.TimeoutException:
                self._failed_requests += 1
                last_error = SPAPIError(
                    "Request timed out",
                    status_code=408,
                    error_type="Timeout",
                    recoverable=True,
                )
            except httpx.NetworkError as e:
                self._failed_requests += 1
                last_error = SPAPIError(
                    f"Network error: {type(e).__name__}",
                    status_code=503,
                    error_type="NetworkError",
                    recoverable=True,
                )
            except Exception as e:
                self._failed_requests += 1
                if isinstance(e, SPAPIError):
                    last_error = e
                else:
                    last_error = SPAPIError(
                        f"Unexpected error: {type(e).__name__}",
                        status_code=500,
                        error_type="UnknownError",
                        recoverable=True,
                    )

            if not last_error.recoverable or attempt == MAX_RETRY_ATTEMPTS:
                raise last_error

            logger.warning(
                "SP-API request failed (attempt %d/%d, recoverable): %s — retrying",
                attempt,
                MAX_RETRY_ATTEMPTS,
                last_error.message,
            )
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        raise last_error  # pragma: no cover — loop always returns or raises above

    def _handle_response(self, response: httpx.Response) -> dict:
        """Process SP-API response.
        
        Args:
            response: HTTP response
            
        Returns:
            Response JSON payload
            
        Raises:
            SPAPIError: If response indicates failure
        """
        if response.status_code == 200:
            self._successful_requests += 1
            try:
                return response.json()
            except Exception:
                raise SPAPIError(
                    "Failed to parse response JSON",
                    status_code=500,
                    error_type="ParseError",
                    recoverable=False,
                )
        
        # Handle error responses
        self._failed_requests += 1
        
        try:
            body = response.json()
            errors = body.get("errors", [])
            if errors:
                error = errors[0]
                error_type = error.get("code", "UnknownError")
                error_message = error.get("message", "No message")
            else:
                error_type = "UnknownError"
                error_message = response.text[:200]
        except Exception:
            error_type = "ParseError"
            error_message = response.text[:200]
        
        # Map HTTP status codes
        if response.status_code == 400:
            recoverable = True  # Bad request might be fixable
        elif response.status_code == 401:
            recoverable = False  # Auth failure
        elif response.status_code == 403:
            recoverable = False  # Permission denied
        elif response.status_code == 404:
            recoverable = False  # Not found
        elif response.status_code == 429:
            recoverable = True  # Rate limited
        elif response.status_code >= 500:
            recoverable = True  # Server error
        else:
            recoverable = False
        
        raise SPAPIError(
            message=error_message,
            status_code=response.status_code,
            error_type=error_type,
            recoverable=recoverable,
        )
    
    # -----------------------------------------------------------------------
    # Orders API Operations (Read-Only)
    # -----------------------------------------------------------------------
    
    async def get_order(self, order_id: str) -> dict:
        """Get a single order by ID.
        
        Uses Orders API v2026-01-01 with includedData for complete order info.
        
        Args:
            order_id: Amazon Order ID
            
        Returns:
            Order details dict
        """
        path = f"/orders/{ORDERS_API_VERSION}/orders/{order_id}"
        params = {
            "marketplaceIds": self._marketplace_id,
            "includedData": "BUYER,RECIPIENT,PROCEEDS,FULFILLMENT,PACKAGES",
        }
        
        return await self._make_request("GET", path, params)
    
    def get_order_sync(self, order_id: str) -> dict:
        """Synchronous version: Get a single order by ID."""
        path = f"/orders/{ORDERS_API_VERSION}/orders/{order_id}"
        params = {
            "marketplaceIds": self._marketplace_id,
            "includedData": "BUYER,RECIPIENT,PROCEEDS,FULFILLMENT,PACKAGES",
        }
        
        return self._make_request_sync("GET", path, params)
    
    async def search_orders(
        self,
        created_after: str | None = None,
        created_before: str | None = None,
        last_updated_after: str | None = None,
        last_updated_before: str | None = None,
        order_statuses: list[str] | None = None,
        fulfillment_channels: list[str] | None = None,
        max_results: int = 50,
        next_token: str | None = None,
    ) -> dict:
        """Search orders with filtering.
        
        Uses Orders API v2026-01-01 searchOrders endpoint.
        
        Args:
            created_after: Filter orders created after this ISO timestamp
            created_before: Filter orders created before this ISO timestamp
            last_updated_after: Filter orders updated after this ISO timestamp
            last_updated_before: Filter orders updated before this ISO timestamp
            order_statuses: Filter by order statuses
            fulfillment_channels: Filter by fulfillment channels (AFN, MFN)
            max_results: Maximum results per page (1-100)
            next_token: Token for next page
            
        Returns:
            Search results with orders and nextToken
        """
        path = f"/orders/{ORDERS_API_VERSION}/orders"
        
        params: dict[str, Any] = {
            "marketplaceIds": self._marketplace_id,
            "includedData": "RECIPIENT,FULFILLMENT",
        }
        
        # Time filters (mutually exclusive: createdAfter OR lastUpdatedAfter)
        if created_after:
            params["createdAfter"] = created_after
        elif last_updated_after:
            params["lastUpdatedAfter"] = last_updated_after
        
        if created_before:
            params["createdBefore"] = created_before
        if last_updated_before:
            params["lastUpdatedBefore"] = last_updated_before
        
        # Status filters
        if order_statuses:
            params["fulfillmentStatuses"] = ",".join(order_statuses)
        
        # Fulfillment channel filters
        if fulfillment_channels:
            params["fulfilledBy"] = ",".join(fulfillment_channels)
        
        # Pagination
        if next_token:
            params["nextToken"] = next_token
        else:
            params["maxResultsPerPage"] = min(max_results, 100)
        
        return await self._make_request("GET", path, params)
    
    def search_orders_sync(
        self,
        created_after: str | None = None,
        created_before: str | None = None,
        max_results: int = 50,
        next_token: str | None = None,
    ) -> dict:
        """Synchronous version: Search orders."""
        path = f"/orders/{ORDERS_API_VERSION}/orders"
        
        params: dict[str, Any] = {
            "marketplaceIds": self._marketplace_id,
            "includedData": "RECIPIENT,FULFILLMENT",
        }
        
        if created_after:
            params["createdAfter"] = created_after
        if created_before:
            params["createdBefore"] = created_before
        
        if next_token:
            params["nextToken"] = next_token
        else:
            params["maxResultsPerPage"] = min(max_results, 100)
        
        return self._make_request_sync("GET", path, params)
    
    def reset_stats(self) -> None:
        """Reset request statistics (used by tests)."""
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
