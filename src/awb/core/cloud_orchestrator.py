from __future__ import annotations

import os
import time
from contextlib import nullcontext
from uuid import uuid4

from awb.providers.providers import OpenAIProvider

from .cloud_budget import (
    budget_snapshot,
    cloud_budget_lock,
    cost_from_usage,
    load_control,
    reserve_cost_eur,
    role_cloud_config,
)
from .models import Task
from .orchestrator import Orchestrator


class CloudAwareOrchestrator(Orchestrator):
    """Hot-switch important calls to a strictly metered OpenAI burst.

    Every model-call boundary re-reads both project and global monthly controls.
    Once either ceiling cannot reserve the next call, routing fails closed to local
    inference. An in-flight paid call is never duplicated or blindly retried.
    """

    def _cloud_important(self, role: str, task: Task | None) -> bool:
        control = load_control(self.workspace.root)
        snap = budget_snapshot(self.workspace.root)
        if not control.enabled or control.mode == 'paused' or control.budget_eur <= 0 or snap.get('hard_blocked'):
            return False
        if not os.getenv('OPENAI_API_KEY') or role not in control.roles or task is None:
            return False
        if control.mode == 'force':
            return True
        attempts = max(
            int(task.metadata.get('scientific_attempts', task.metadata.get('attempts', 0))),
            int(task.metadata.get('technical_failures', 0)),
        )
        return task.priority >= control.priority_threshold or attempts > 0 or bool(task.metadata.get('critical_objections'))

    @staticmethod
    def _usage(provider: OpenAIProvider) -> tuple[int, int]:
        usage = provider.last_usage or {}
        input_tokens = int(usage.get('input_tokens') or usage.get('prompt_tokens') or 0)
        output_tokens = int(usage.get('output_tokens') or usage.get('completion_tokens') or 0)
        return input_tokens, output_tokens

    def _cloud_call(self, role: str, system: str, user: str, task: Task) -> str | None:
        config = role_cloud_config(role, self.workspace.root)
        candidates = [config]
        if config['model'] == 'gpt-5.6-sol':
            candidates.append({**config, 'model': 'gpt-5.6-terra'})
        if role in {'director', 'verifier'}:
            candidates.append({
                **config,
                'model': 'gpt-5.6-luna',
                'reasoning': 'low',
                'max_output_tokens': min(1200, int(config['max_output_tokens'])),
            })

        chosen = None
        reserve = None
        reservation_id = None
        snap = None
        control = None
        # The reservation boundary is serialized across every project/process.
        # Outstanding reservations are persisted before the request starts, so a
        # crashed/killed paid call fails closed instead of silently freeing budget.
        with cloud_budget_lock(self.workspace.root):
            control = load_control(self.workspace.root)
            snap = budget_snapshot(self.workspace.root)
            if (
                not control.enabled
                or control.mode == 'paused'
                or snap.get('hard_blocked')
                or role not in control.roles
                or not os.getenv('OPENAI_API_KEY')
            ):
                return None
            remaining = float(snap['remaining_eur'])
            for candidate in candidates:
                needed = reserve_cost_eur(candidate['model'], system, user, int(candidate['max_output_tokens']))
                if needed <= remaining:
                    chosen, reserve = candidate, needed
                    break
            if chosen is None:
                self.ledger.event('cloud_budget_exhausted', {
                    'role': role,
                    'mode': control.mode,
                    'budget_eur': control.budget_eur,
                    'spent_eur': snap['spent_eur'],
                    'reserved_eur': snap.get('reserved_eur', 0.0),
                    'monthly_budget_eur': snap.get('monthly_budget_eur'),
                    'monthly_spent_eur': snap.get('monthly_spent_eur'),
                    'monthly_reserved_eur': snap.get('monthly_reserved_eur', 0.0),
                    'remaining_eur': remaining,
                    'action': 'fall_back_to_local',
                }, task.id)
                return None
            reservation_id = uuid4().hex
            self.ledger.event('cloud_budget_reserved', {
                'reservation_id': reservation_id,
                'amount_eur': round(float(reserve), 6),
                'role': role,
                'model': chosen['model'],
                'mode': control.mode,
            }, task.id)

        assert chosen is not None and reserve is not None and reservation_id is not None
        remaining_before = float(snap['remaining_eur']) if snap else 0.0
        provider = OpenAIProvider(chosen['model'])
        provider.max_output_tokens = int(chosen['max_output_tokens'])
        provider.reasoning_effort = str(chosen['reasoning'])
        meta = self._route_meta(role, 'openai-cloud-burst', 'openai', chosen['model'], 'cloud-burst')
        started = time.monotonic()
        self.ledger.event('model_escalated', {
            'role': role,
            'task_id': task.id,
            'to': meta,
            'mode': control.mode if control else 'auto',
            'budget_eur': control.budget_eur if control else 0.0,
            'monthly_budget_eur': snap.get('monthly_budget_eur') if snap else None,
            'remaining_before_eur': remaining_before,
            'reserved_eur': round(float(reserve), 6),
            'reservation_id': reservation_id,
            'reasoning_effort': chosen['reasoning'],
            'max_output_tokens': chosen['max_output_tokens'],
        }, task.id)
        self.ledger.event('model_call_started', meta, task.id)
        try:
            result = provider.generate(system, user)
        except Exception as exc:
            elapsed = time.monotonic() - started
            error = f'{type(exc).__name__}: {exc}'
            self._persist_model_call(meta, task.id, success=False, seconds=elapsed, error=error)
            self._attempt_routes.append({**meta, 'success': False, 'seconds': round(elapsed, 3), 'error': error})
            self.ledger.event('model_call_failed', {**meta, 'error': error, 'seconds': round(elapsed, 3)}, task.id)
            self.ledger.event('cloud_call_fallback_local', {
                'role': role,
                'error': error,
                'reservation_id': reservation_id,
                'reserved_eur': round(float(reserve), 6),
                'reservation_held_fail_closed': True,
            }, task.id)
            # Deliberately do not release the reservation on an ambiguous paid-call
            # failure: the provider may already have billed part/all of the request.
            return None

        elapsed = time.monotonic() - started
        input_tokens, output_tokens = self._usage(provider)
        try:
            cost_usd, cost_eur = cost_from_usage(chosen['model'], input_tokens, output_tokens)
        except ValueError:
            cost_usd = 0.0
            # Fail closed: charge the full reservation when pricing cannot be
            # reconstructed instead of allowing another potentially over-budget call.
            cost_eur = float(reserve)
        usage_event = {
            'role': role,
            'model': chosen['model'],
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost_usd': round(cost_usd, 6),
            'cost_eur': round(cost_eur, 6),
            'budget_eur': control.budget_eur if control else 0.0,
            'monthly_budget_eur': snap.get('monthly_budget_eur') if snap else None,
            'response_id': provider.last_response_id,
            'reservation_id': reservation_id,
        }
        with cloud_budget_lock(self.workspace.root):
            # Usage is written before the max reservation is released. There is no
            # window in which another process can spend the same budget twice.
            self.ledger.event('cloud_usage_metered', usage_event, task.id)
            self.ledger.event('cloud_budget_released', {
                'reservation_id': reservation_id,
                'reserved_eur': round(float(reserve), 6),
                'actual_eur': round(float(cost_eur), 6),
                'reason': 'usage_recorded',
            }, task.id)
        chars = len(result)
        self._persist_model_call(meta, task.id, success=True, seconds=elapsed, chars=chars)
        call_meta = {
            **meta, 'success': True, 'seconds': round(elapsed, 3), 'chars': chars,
            'chars_per_second': round(chars / elapsed, 1) if elapsed > 0 else None,
            'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cost_eur': round(cost_eur, 6),
        }
        self.ledger.event('model_call_finished', call_meta, task.id)
        self._attempt_routes.append(call_meta)
        return result

    def _call_model(self, role, system, user, task: Task | None = None):
        task_id = task.id if task else None
        if self.provider_override is not None:
            return super()._call_model(role, system, user, task)

        if task is not None and self._cloud_important(role, task):
            cloud_result = self._cloud_call(role, system, user, task)
            if cloud_result is not None:
                return cloud_result

        calls = []
        for route in self.router.candidates(role):
            calls.append((None, route, self._route_meta(role, route.node_id, route.kind, route.model, route.source)))
        if not calls:
            raise RuntimeError(f'No healthy model route available for role {role}')

        last_exc = None
        for index, (provider, route, meta) in enumerate(calls):
            started = time.monotonic()
            self.ledger.event('model_call_started', meta, task_id)
            try:
                if provider is None:
                    provider = self.router.provider(route)
                if hasattr(provider, 'configure_role'):
                    provider.configure_role(role)
                context = self.router.slot(route) if route is not None else nullcontext()
                with context:
                    result = provider.generate(system, user)
            except Exception as exc:
                last_exc = exc
                elapsed = time.monotonic() - started
                error = f'{type(exc).__name__}: {exc}'
                if route is not None:
                    self.router.record_failure(route)
                self._persist_model_call(meta, task_id, success=False, seconds=elapsed, error=error)
                self._attempt_routes.append({**meta, 'success': False, 'seconds': round(elapsed, 3), 'error': error})
                self.ledger.event('model_call_failed', {**meta, 'error': error, 'seconds': round(elapsed, 3)}, task_id)
                if index + 1 < len(calls):
                    self.ledger.event('model_route_failover', {
                        'role': role,
                        'failed_node': meta['node'],
                        'next_node': calls[index + 1][2]['node'],
                    }, task_id)
                    continue
                raise
            elapsed = time.monotonic() - started
            chars = len(result)
            if route is not None:
                self.router.record_success(route, seconds=elapsed, chars=chars)
            self._persist_model_call(meta, task_id, success=True, seconds=elapsed, chars=chars)
            call_meta = {
                **meta, 'success': True, 'seconds': round(elapsed, 3), 'chars': chars,
                'chars_per_second': round(chars / elapsed, 1) if elapsed > 0 else None,
            }
            self.ledger.event('model_call_finished', call_meta, task_id)
            self._attempt_routes.append(call_meta)
            return result
        if last_exc:
            raise last_exc
        raise RuntimeError(f'No model route available for role {role}')
