"""
Connector recipes API — pre-configured data source templates.

Endpoints:
  GET  /recipes           — List all available recipes (auth required)
  GET  /recipes/{id}      — Get single recipe (auth required)
  POST /recipes/{id}/enable — Enable a recipe (auth required, creates connector)
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from storage.connectors.recipes import get_all_recipes, get_recipe, instantiate_recipe
from storage.connectors.manager import create_connector
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/recipes", tags=["recipes"])


@router.get("")
async def list_recipes(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """List all available connector recipes."""
    recipes = get_all_recipes()
    return {
        "recipes": recipes,
        "count": len(recipes),
        "categories": sorted(set(r["category"] for r in recipes)),
    }


@router.get("/{recipe_id}")
async def get_one(recipe_id: str, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Get a single recipe."""
    recipe = get_recipe(recipe_id)
    if not recipe:
        return JSONResponse({"error": "recipe not found"}, status_code=404)
    return recipe


@router.post("/{recipe_id}/enable")
async def enable_recipe(
    recipe_id: str,
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Enable a recipe — creates a real connector from the template."""
    body = await request.json()
    user_params = body.get("params", {})

    # Validate required params
    recipe = get_recipe(recipe_id)
    if not recipe:
        return JSONResponse({"error": "recipe not found"}, status_code=404)

    for p in recipe.get("user_params", []):
        if p.get("required") and p["name"] not in user_params:
            if "default" in p:
                user_params[p["name"]] = p["default"]
            else:
                return JSONResponse(
                    {"error": f"Missing required parameter: {p['label']}"},
                    status_code=400,
                )

    # Instantiate the recipe
    instance = instantiate_recipe(recipe_id, user_params)
    if not instance:
        return JSONResponse({"error": "failed to instantiate recipe"}, status_code=500)

    # Create the actual connector
    conn_config = instance["connector_config"]
    device_id = f"recipe_{recipe_id}_{auth.tenant_id}"

    # Include signal_type_hint in source_config so the ingestion pipeline
    # can use it to classify signals and promote them to the signals table
    source_config = conn_config["source_config"]
    if instance.get("signal_type_hint"):
        source_config["signal_type_hint"] = instance["signal_type_hint"]

    try:
        result = create_connector(
            tenant_id=auth.tenant_id,
            device_id=device_id,
            connector_type=conn_config.get("connector_type", "pull"),
            source_type=conn_config["source_type"],
            source_config=source_config,
            poll_interval=conn_config.get("poll_interval", "5 minutes"),
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({
        "enabled": True,
        "recipe_id": recipe_id,
        "recipe_name": instance["name"],
        "connector_id": result.get("connector_id"),
        "device_id": device_id,
    }, status_code=201)
