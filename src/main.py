# main.py
# FastAPI entry point for the validator

from datetime import timedelta
import os
from fastapi import Depends, FastAPI, Header
from typing import Annotated, List
import json
import time
from contextlib import asynccontextmanager
import asyncio
from fastapi.middleware.cors import CORSMiddleware

from fiber import SubstrateInterface

from .mapping import MappingManager

from .tasks import set_weights_task, sync_hotkey_workers_task

from .utils import is_hotkey_registered, verify_signature

from .interfaces.worker_provider import WorkerProvider

from .validator import Validator

from .models import HotkeyWorkerRegistration, MetricsResponse, UnbindWorkerRequest

from .config import ValidatorSettings, load_config
from fiber.chain import chain_utils
from fiber.utils import get_logger

from .dependencies import (
    get_database_service,
    get_dynamic_config_service,
    get_mapping_manager,
    get_mapping_source,
    get_metrics_client,
    get_substrate,
    get_validator,
    get_worker_provider,
)
from .interfaces.database import DatabaseService
from . import __version__ as version


logger = get_logger(__name__)


# CORS setup
ENV = os.environ.get("ENV", "prod")
REMOTE_SITE_ORIGIN = os.environ.get(
    "REMOTE_SITE_ORIGIN", "https://hashtensor.com"
)

if ENV == "test":
    origins = ["http://localhost", "http://localhost:3000", "http://127.0.0.1"]
else:
    origins = [REMOTE_SITE_ORIGIN]


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    dynamic_config_service = get_dynamic_config_service(config)
    metrics_client = get_metrics_client(config)
    mapping_source = get_mapping_source(config)
    mapping_manager = get_mapping_manager(mapping_source, config)
    validator = get_validator(
        config,
        metrics_client,
        mapping_manager,
    )
    substrate = get_substrate(config)
    keypair = chain_utils.load_hotkey_keypair(
        wallet_name=config.wallet_name, hotkey_name=config.wallet_hotkey
    )
    db_service = get_database_service(config)

    # --- Start of Modified Logic for Background Tasks ---
    shutdown_event = asyncio.Event()

    async def weights_loop_wrapper():
        while not shutdown_event.is_set():
            try:
                # Use asyncio.to_thread for blocking calls like the original asyncio.run,
                # ensuring it doesn't block the main event loop directly.
                # However, if set_weights_task itself is fully async, to_thread might not be needed.
                # Assuming set_weights_task might contain blocking parts based on original `asyncio.run`.
                await asyncio.to_thread(
                    lambda: asyncio.run( # This asyncio.run here is redundant if weights_task is already async
                        set_weights_task(
                            dynamic_config_service,
                            config,
                            validator,
                            substrate,
                            keypair,
                        )
                    )
                )
                await asyncio.sleep(timedelta(minutes=1).total_seconds()) # Original sleep time
            except asyncio.CancelledError:
                logger.info("Weights task cancelled.")
                break
            except Exception as e:
                logger.exception(f"Error in set_weights task: {e}")
                # Decide if you want to exit on error or keep trying
                # For now, let's just log and continue, unless it's critical.
                await asyncio.sleep(5) # short pause before retrying
        logger.info("Weights loop stopped.")


    async def sync_hotkey_workers_loop_wrapper():
        while not shutdown_event.is_set():
            try:
                await asyncio.to_thread(
                    lambda: asyncio.run( # This asyncio.run here is redundant if sync_task is already async
                        sync_hotkey_workers_task(db_service, config, substrate)
                    )
                )
                await asyncio.sleep(
                    timedelta(minutes=config.sync_hotkey_workers_interval).total_seconds()
                )
            except asyncio.CancelledError:
                logger.info("Sync hotkey workers task cancelled.")
                break
            except Exception as e:
                logger.exception(f"Error in sync_hotkey_workers_task: {e}")
                # Decide if you want to exit on error or keep trying
                await asyncio.sleep(5) # short pause before retrying
        logger.info("Sync hotkey workers loop stopped.")


    tasks = []
    if not config.disable_set_weights:
        tasks.append(asyncio.create_task(weights_loop_wrapper()))
        logger.info("Set weights task started")
    else:
        logger.info("Set weights task disabled by DISABLE_SET_WEIGHTS flag")
    
    tasks.append(asyncio.create_task(sync_hotkey_workers_loop_wrapper()))
    logger.info("Sync hotkey workers task started")

    try:
        yield
    finally:
        logger.info("Application shutdown initiated. Signalling background tasks to stop.")
        shutdown_event.set()
        # Give tasks a moment to recognize the shutdown event and finish gracefully
        for task in tasks:
            task.cancel()
        # Wait for all tasks to complete or be cancelled. Timeout to prevent indefinite hang.
        await asyncio.gather(*tasks, return_exceptions=True) # return_exceptions so it doesn't fail if one task failed
        logger.info("Background tasks stopped.")
    # --- End of Modified Logic for Background Tasks ---


app = FastAPI(
    prefix="/api",
    title="HashTensor Validator",
    lifespan=lifespan,
    version=version,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        ["*"] if ENV == "test" else [REMOTE_SITE_ORIGIN]
    ),  # Not recommended for production!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    return {"status": "OK"}


@app.post("/register")
async def register_hotkey_worker(
    reg: HotkeyWorkerRegistration,
    db_service: Annotated[DatabaseService, Depends(get_database_service)],
    worker_provider: Annotated[WorkerProvider, Depends(get_worker_provider)],
    config: Annotated[ValidatorSettings, Depends(load_config)],
    substrate: Annotated[SubstrateInterface, Depends(get_substrate)],
    x_signature: Annotated[str, Header(alias="X-Signature")],
):
    # 1. Log incoming request data
    logger.debug(f"/register request: payload={reg.model_dump()}")
    reg_dict = reg.model_dump()
    reg_json = json.dumps(reg_dict, sort_keys=True, separators=(",", ":"))
    logger.debug(f"/register reg_json: {reg_json}")
    logger.debug(f"/register X-Signature: {x_signature}")
    now = time.time()
    if (
        abs(now - reg.registration_time)
        > config.registration_time_tolerance.total_seconds()
    ):
        raise HTTPException(
            status_code=400,
            detail="Registration time is too far from current UTC time.",
        )
    # 2. Security check: worker name must contain the hotkey
    if reg.hotkey not in reg.worker:
        raise HTTPException(
            status_code=400,
            detail="Worker name must contain the hotkey for security validation.",
        )
    # 3. Check worker exists
    if not await worker_provider.is_worker_exists(
        config.kaspa_pool_owner_wallet, reg.worker
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Worker not found. Make sure you are using the correct wallet address\n"
            + f"Kaspa Pool Owner Wallet: {config.kaspa_pool_owner_wallet}",
        )
    # 4. Verify signature on the full request object (sorted keys)
    if config.verify_signature and not verify_signature(
        reg.hotkey, reg_json, x_signature
    ):
        raise HTTPException(status_code=400, detail="Invalid signature")
    # 5. Check hotkey is registered
    if not is_hotkey_registered(reg.hotkey, substrate, config.netuid):
        raise HTTPException(
            status_code=400,
            detail="Hotkey not registered. To register in subnet use btcli command: `btcli subnet register`",
        )
    try:
        # Store registration_time as float seconds, db will convert to int microseconds
        await db_service.add_mapping(
            reg.hotkey, reg.worker, x_signature, reg.registration_time
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dict(message="Registration successful")


@app.get("/metrics")
async def get_metrics(
    validator: Annotated[Validator, Depends(get_validator)]
) -> List[MetricsResponse]:
    hotkey_metrics = await validator.get_hotkey_metrics_map()
    return [
        MetricsResponse(
            hotkey=hotkey,
            active_workers=len([m for m in metrics if m.uptime > 0]),
            total_workers=len(metrics),
            metrics=metrics,
        )
        for hotkey, metrics in hotkey_metrics.items()
    ]


@app.get("/mappings")
async def get_mappings(
    mapping_manager: Annotated[MappingManager, Depends(get_mapping_manager)]
):
    return await mapping_manager.get_mapping()


@app.get("/hotkey_workers")
async def get_hotkey_workers(
    db_service: Annotated[DatabaseService, Depends(get_database_service)],
    since_timestamp: float = 0.0,
    page_size: int = 100,
    page_number: int = 1,
):
    return await db_service.get_hotkey_workers_by_time(
        since_timestamp=since_timestamp,
        page_size=page_size,
        page_number=page_number,
    )


# Only define /ratings if ENV == "test"
if ENV == "test":

    @app.get("/ratings")
    async def get_ratings(
        validator: Annotated[Validator, Depends(get_validator)]
    ):
        return await validator.compute_ratings()


@app.post("/unbind")
async def unbind_worker(
    req: UnbindWorkerRequest,
    db_service: Annotated[DatabaseService, Depends(get_database_service)],
    config: Annotated[ValidatorSettings, Depends(load_config)],
    substrate: Annotated[SubstrateInterface, Depends(get_substrate)],
    x_signature: Annotated[str, Header(alias="X-Signature")],
):
    # Log request
    logger.debug(f"/unbind request: payload={req.model_dump()}")
    req_dict = req.model_dump()
    req_json = json.dumps(req_dict, sort_keys=True, separators=(",", ":"))
    logger.debug(f"/unbind req_json: {req_json}")
    logger.debug(f"/unbind X-Signature: {x_signature}")
    # Verify signature on the full request object (sorted keys)
    if config.verify_signature and not verify_signature(
        req.hotkey, req_json, x_signature
    ):
        raise HTTPException(status_code=400, detail="Invalid signature")
    # Check hotkey is registered
    if not is_hotkey_registered(req.hotkey, substrate, config.netuid):
        raise HTTPException(
            status_code=400,
            detail="Hotkey not registered. To register in subnet use btcli command: `btcli subnet register`",
        )
    try:
        await db_service.mark_worker_unbound(
            req.hotkey, req.worker, x_signature
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return dict(message="Worker unbound successfully")
