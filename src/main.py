import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List

import bittensor as bt
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from prometheus_client import start_http_server, Gauge
from pydantic import BaseModel, Field

from src.chain_utils import get_current_block, load_hotkey_keypair
from src.config import Config
from src.interface import (
    get_all_validators_and_endpoints,
    get_all_miners_and_endpoints,
    get_hotkey_workers,
    get_metagraph,
    get_validator_sync_offset,
    update_validator_sync_offset,
    add_hotkey_worker_mapping,
    mark_hotkey_worker_unbound,
    get_balance,
)
from src.set_weights import set_weights
from src.metrics import get_metrics
from src.interfaces.database import DatabaseService, DynamicConfigService

# Configuration setup
config = Config()

# Initialize Bittensor wallet
wallet = load_hotkey_keypair(
    config.validator_wallet_name, config.validator_wallet_hotkey
)
bt.logging.info(f"Wallet: {wallet.hotkey_str}")


class WorkerMapping(BaseModel):
    worker: str = Field(..., description="The worker's SS58 address.")
    hotkey: str = Field(..., description="The hotkey's SS58 address.")
    signature: str = Field(..., description="The signature of the worker registration.")
    registration_time: float = Field(
        ..., description="The unix timestamp of registration."
    )


class UnbindRequest(BaseModel):
    worker: str = Field(..., description="The worker's SS58 address to unbind.")
    hotkey: str = Field(..., description="The hotkey's SS58 address.")
    unbind_signature: str = Field(
        ..., description="The signature for the unbind operation."
    )


# FastAPI app
app = FastAPI()

# Database services
database_service = DatabaseService(db_url=config.database_url, max_workers=config.max_workers_per_hotkey)
dynamic_config_service = DynamicConfigService(db_url=config.database_url)

# Prometheus metrics
g_active_workers = Gauge(
    "active_workers",
    "Number of active worker registrations for the validator's hotkey.",
    ["hotkey"],
)
g_balance = Gauge(
    "hotkey_balance",
    "Balance of the validator's hotkey in Tao.",
    ["hotkey"],
)
g_vali_sync_offset = Gauge(
    "validator_sync_offset",
    "Last synced registration time from remote validator APIs.",
    ["hotkey", "remote_validator_hotkey"],
)
g_worker_sync_latency = Gauge(
    "worker_sync_latency_seconds",
    "Latency of fetching workers from a remote validator API in seconds.",
    ["hotkey", "remote_validator_hotkey"],
)


@app.get("/", response_class=HTMLResponse)
async def read_root():
    return """
    <html>
        <head>
            <title>HashTensor Validator API</title>
        </head>
        <body>
            <h1>HashTensor Validator API</h1>
            <p>Welcome to the HashTensor Validator API. Navigate to /docs for API documentation.</p>
            <p>Check metrics at /metrics</p>
        </body>
    </html>
    """


@app.post("/register_worker")
async def register_worker(worker_data: WorkerMapping):
    try:
        await add_hotkey_worker_mapping(
            worker_data.hotkey,
            worker_data.worker,
            worker_data.signature,
            worker_data.registration_time,
            database_service,
        )
        return {"message": "Worker registered successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        bt.logging.error(f"Error registering worker: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/unbind_worker")
async def unbind_worker(unbind_data: UnbindRequest):
    try:
        await mark_hotkey_worker_unbound(
            unbind_data.hotkey,
            unbind_data.worker,
            unbind_data.unbind_signature,
            database_service,
        )
        return {"message": "Worker unbound successfully"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        bt.logging.error(f"Error unbinding worker: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/hotkey_workers")
async def get_all_hotkey_workers(
    since_timestamp: float = 0.0, page_size: int = 100, page_number: int = 1
) -> List[Dict[str, Any]]:
    try:
        workers = await database_service.get_hotkey_workers_by_time(
            since_timestamp, page_size, page_number
        )
        return workers
    except Exception as e:
        bt.logging.error(f"Error fetching hotkey workers: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/metrics")
async def get_metrics_endpoint():
    return get_metrics(
        wallet=wallet,
        database_service=database_service,
        g_active_workers=g_active_workers,
        g_balance=g_balance,
        subtensor_interface=get_metagraph,
        get_balance=get_balance,
    )


async def set_weights_task(wallet: bt.wallet):
    """
    Sets weights for the hotkey on the Bittensor network based on miner performance.
    This runs periodically.
    """
    bt.logging.info(f"Setting weights for {wallet.hotkey_str}")
    try:
        # Fetch current block height
        current_block = await get_current_block()
        if current_block is None:
            bt.logging.warning("Could not fetch current block. Skipping weight set.")
            return

        # Fetch metagraph and set weights
        metagraph = await get_metagraph(netuid=config.netuid)
        await set_weights(
            wallet=wallet,
            netuid=config.netuid,
            metagraph=metagraph,
            subtensor_interface=get_metagraph,  # Passed for internal use if needed
            config=config,
            db_service=database_service,
            dynamic_config_service=dynamic_config_service
        )
    except Exception as e:
        bt.logging.error(f"Failed to set weights: {e}")


async def sync_hotkey_workers_task(wallet: bt.wallet):
    """
    Synchronizes hotkey workers by fetching data from other validator APIs
    and updating the local database.
    """
    bt.logging.info("[sync_hotkey_workers_task] Starting sync task...")
    try:
        metagraph = await get_metagraph(netuid=config.netuid)
        all_validators = await get_all_validators_and_endpoints(metagraph)

        bt.logging.info(
            f"[sync_hotkey_workers_task] Found {len(all_validators)} endpoints to check."
        )

        checked_endpoints = 0
        valid_hashtensor_endpoints = []
        for validator_hotkey, api_url in all_validators:
            if not api_url:
                continue
            # Basic check to see if the endpoint is a HashTensor validator
            # This is a simple /openapi.json check; more robust checks might be needed
            try:
                # Use a small timeout for this initial check
                response = await asyncio.wait_for(get_hotkey_workers(api_url + "/openapi.json", raise_for_status=False), timeout=2)
                if response and response.status_code == 200:
                    valid_hashtensor_endpoints.append((validator_hotkey, api_url))
            except (asyncio.TimeoutError, Exception) as e:
                # bt.logging.debug(f"Skipping {api_url} (not an OpenAPI endpoint or timeout): {e}")
                pass # Suppress frequent debug logs for non-openAPI endpoints

        bt.logging.info(
            f"[sync_hotkey_workers_task] {len(valid_hashtensor_endpoints)} endpoints are valid HashTensor Validators."
        )

        total_added = 0
        total_skipped = 0
        total_failed = 0

        for validator_hotkey, api_url in valid_hashtensor_endpoints:
            bt.logging.info(
                f"[sync_hotkey_workers_task] Syncing validator {validator_hotkey} at {api_url}"
            )
            try:
                # Get last synced timestamp for this validator from local DB
                last_synced_time = await get_validator_sync_offset(
                    validator_hotkey, database_service
                )

                # Fetch workers from the remote validator's API
                start_time = time.time()
                remote_workers_data = await get_hotkey_workers(
                    api_url + f"/hotkey_workers?since_timestamp={last_synced_time}"
                )
                latency = time.time() - start_time
                g_worker_sync_latency.labels(
                    hotkey=wallet.hotkey_str, remote_validator_hotkey=validator_hotkey
                ).set(latency)

                bt.logging.info(
                    f"[sync_hotkey_workers_task] Fetched {len(remote_workers_data)} workers from {validator_hotkey}"
                )

                max_registration_time_in_batch = last_synced_time

                for worker_data in remote_workers_data:
                    worker = worker_data["worker"]
                    hotkey = worker_data["hotkey"]
                    signature = worker_data["signature"]
                    registration_time = worker_data["registration_time"]

                    # Basic sanity check: ensure worker name contains the hotkey
                    # This adds a layer of security to ensure the worker claims a hotkey
                    if hotkey not in worker:
                        bt.logging.warning(
                            f"Worker name {worker} does not contain hotkey {hotkey} - skipping for security"
                        )
                        total_skipped += 1
                        continue

                    try:
                        await add_hotkey_worker_mapping(
                            hotkey,
                            worker,
                            signature,
                            registration_time,
                            database_service,
                        )
                        bt.logging.info(f"Added worker {worker} from remote validator API")
                        total_added += 1
                        max_registration_time_in_batch = max(
                            max_registration_time_in_batch, registration_time
                        )
                    except ValueError as e:
                        # Worker already registered or max workers reached for hotkey
                        # This is expected for existing workers, not an error.
                        # bt.logging.debug(f"Skipping worker {worker} ({e})")
                        total_skipped += 1
                    except Exception as e:
                        bt.logging.error(f"Failed to add worker {worker}: {e}")
                        total_failed += 1

                # Update sync offset only if new workers were successfully added
                if max_registration_time_in_batch > last_synced_time:
                    await update_validator_sync_offset(
                        validator_hotkey, max_registration_time_in_batch, database_service
                    )
                    g_vali_sync_offset.labels(
                        hotkey=wallet.hotkey_str, remote_validator_hotkey=validator_hotkey
                    ).set(max_registration_time_in_batch)

                bt.logging.info(
                    f"[sync_hotkey_workers_task] {validator_hotkey}: Added: {total_added}, Skipped: {total_skipped}, Failed: {total_failed}"
                )

            except Exception as e:
                bt.logging.error(
                    f"[sync_hotkey_workers_task] Error syncing from {validator_hotkey} at {api_url}: {e}"
                )
                total_failed += len(remote_workers_data) # Estimate failed for connection issues

        bt.logging.info(
            f"[sync_hotkey_workers_task] Done. Total Added: {total_added}, Total Skipped: {total_skipped}, Total Failed: {total_failed}"
        )

    except Exception as e:
        bt.logging.error(f"Error in sync_hotkey_workers_task: {e}", exc_info=True)


async def set_weights_loop_wrapper(wallet: bt.wallet):
    while True:
        await set_weights_task(wallet)
        # --- PREVIOUS CODE ---
        # await asyncio.sleep(timedelta(minutes=config.set_weights_interval).total_seconds())
        # --- UPDATED CODE ---
        await asyncio.sleep(config.set_weights_interval.total_seconds())
        # --- END UPDATED CODE ---

async def sync_hotkey_workers_loop_wrapper(wallet: bt.wallet):
    while True:
        await sync_hotkey_workers_task(wallet)
        # --- PREVIOUS CODE ---
        # await asyncio.sleep(timedelta(minutes=config.sync_hotkey_workers_interval).total_seconds())
        # --- UPDATED CODE ---
        await asyncio.sleep(config.sync_hotkey_workers_interval.total_seconds())
        # --- END UPDATED CODE ---


@app.on_event("startup")
async def lifespan():
    bt.logging.info("Waiting for application startup.")
    # Start Prometheus metrics server
    start_http_server(config.prometheus_port)
    bt.logging.info(f"Prometheus metrics server started on port {config.prometheus_port}")

    # Start background tasks
    # The set_weights_interval and sync_hotkey_workers_interval are timedelta objects
    # defined in config.py, so we directly use .total_seconds()
    asyncio.create_task(set_weights_loop_wrapper(wallet))
    bt.logging.info("Set weights task started")
    asyncio.create_task(sync_hotkey_workers_loop_wrapper(wallet))
    bt.logging.info("Sync hotkey workers task started")
    bt.logging.info("Application startup complete.")
    yield
    bt.logging.info("Application shutdown complete.")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=config.port)
