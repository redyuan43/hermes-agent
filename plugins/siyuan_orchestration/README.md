# SIYUAN Orchestration

This standalone plugin owns the Nano/SIYUAN-specific orchestration policy:

- Luna, Terra, and Sol conversation routing with one-turn MoA escalation
- `kanban_create` assignee validation
- fixed Kanban completion delivery and wake mode
- fallback to the task's origin route
- fallback policy state in plugin-owned SQLite

Enable it explicitly:

```bash
hermes plugins enable siyuan-orchestration
```

All business settings live under the plugin namespace:

```yaml
plugins:
  enabled:
    - siyuan-orchestration
  entries:
    siyuan-orchestration:
      settings:
        allowed_assignees: [nano1, reviewer]
        model_routing:
          enabled: true
          profiles:
            luna: {provider: openrouter, model: your-luna-model}
            terra: {provider: openrouter, model: your-terra-model}
            sol: {provider: openrouter, model: your-sol-model}
          moa:
            preset: default
          trace:
            enabled: true
            retention_days: 7
        completion_delivery:
          sender_profile: siyuan-mobile
          platform: telegram
          chat_id: your-chat-id
          thread_id: ""
        wake:
          mode: notify+wake
        fallback:
          enabled: true
          after_attempts: 3
```

The route classifier uses the plugin-owned auxiliary slot:

```yaml
auxiliary:
  siyuan_route_classifier:
    provider: openrouter
    model: your-classifier-model
```

Provider credentials remain in Hermes provider/profile configuration. The
plugin receives only provider and model identities; Hermes resolves secrets
and runtime transport details.

## Migration

- `smart_model_routing` moves to
  `plugins.entries.siyuan-orchestration.settings.model_routing`.
- `kanban.allowed_assignees` moves to
  `plugins.entries.siyuan-orchestration.settings.allowed_assignees`.
- `kanban.completion_delivery` moves to
  `plugins.entries.siyuan-orchestration.settings.completion_delivery`.
- Fallback policy state lives in
  `$HERMES_KANBAN_HOME/plugin-data/siyuan-orchestration/state.db`.

Per-item delivery checkpoints remain in the generic Kanban transport database.
They protect every notifier from duplicate summaries or attachments after a
partial send, including deployments where this plugin is disabled.

The plugin is inert when enabled without settings.
