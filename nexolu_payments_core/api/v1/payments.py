
# The application entrypoint already includes `router`; include the trusted
# provisioning sub-router here so no separate app wiring is required.
router.include_router(provisioning_router)
