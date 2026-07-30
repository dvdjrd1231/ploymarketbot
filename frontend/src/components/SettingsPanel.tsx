import { useEffect, useState } from 'react'
import { useEngineState, useSettings } from '../lib/live'
import { api } from '../lib/api'
import { money, shortAddress } from '../lib/format'
import { Check, Field, Notice } from './common'
import type { Settings } from '../types'

type Draft = Partial<Settings>

/**
 * Configuration.
 *
 * The private key is handled as write-only: it is posted to the server, stored
 * in the OS credential manager, and there is no route that can read it back.
 * The form only ever knows whether *a* key exists.
 */
export function SettingsPanel({
  onError, onInfo,
}: {
  onError: (m: string) => void
  onInfo: (m: string) => void
}) {
  const settings = useSettings()
  const state = useEngineState()
  const [draft, setDraft] = useState<Draft>({})
  const [privateKey, setPrivateKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [connecting, setConnecting] = useState(false)

  // Adopt server values for any field the user has not touched, so a settings
  // frame from another tab does not clobber what is being typed here.
  useEffect(() => {
    if (!settings) return
    setDraft((current) => {
      const next: Draft = { ...current }
      for (const key of Object.keys(settings) as (keyof Settings)[]) {
        if (!(key in current)) next[key] = settings[key] as never
      }
      return next
    })
  }, [settings])

  if (!settings) {
    return <div className="empty"><div className="empty-title">Loading settings…</div></div>
  }

  const value = <K extends keyof Settings>(key: K): Settings[K] =>
    (draft[key] ?? settings[key]) as Settings[K]

  const set = <K extends keyof Settings>(key: K, v: Settings[K]) =>
    setDraft((d) => ({ ...d, [key]: v }))

  const dirty = (Object.keys(draft) as (keyof Settings)[])
    .some((key) => draft[key] !== settings[key])

  const paper = Boolean(value('paper_trading'))
  const live = !paper && !value('dry_run')

  const save = async () => {
    setSaving(true)
    try {
      const patch: Record<string, unknown> = {}
      for (const key of Object.keys(draft) as (keyof Settings)[]) {
        if (draft[key] !== settings[key]) patch[key as string] = draft[key]
      }
      // Server-owned fields are never written back from the form.
      delete patch.hasPrivateKey
      delete patch.secureStorage
      delete patch.storageBackend
      delete patch.my_wallet_address

      const result = await api.saveSettings(patch)
      setDraft({})
      onInfo(result.restartRequired
        ? 'Settings saved. Restart the bot for the wallet/mode change to take effect.'
        : 'Settings saved.')
      if (result.problems?.length) {
        onError(result.problems.join(' '))
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  const saveKey = async () => {
    if (!privateKey.trim()) return
    setSaving(true)
    try {
      const result = await api.saveKey(privateKey.trim(),
        String(value('funder_address') ?? ''))
      setPrivateKey('')
      onInfo(`Key stored securely for ${shortAddress(result.address)} `
        + `(${result.storageBackend}).`)
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  const connect = async () => {
    setConnecting(true)
    try {
      const result = await api.connect(privateKey.trim(),
        String(value('funder_address') ?? ''))
      setPrivateKey('')
      const account = result.account
      onInfo(`Connected as ${shortAddress(account.address)} · `
        + `${account.accountLabel} · balance ${money(account.balance)}`)
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    } finally {
      setConnecting(false)
    }
  }

  const removeKey = async () => {
    if (!window.confirm(
      'Remove the stored wallet key from this computer?\n\n'
      + 'Live and dry-run trading will stop working until you add it again.'
    )) return
    try {
      await api.clearKey()
      onInfo('Stored key removed.')
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    }
  }

  const resetSession = async () => {
    if (!window.confirm(
      'Clear all trades, orders, positions and history?\n\n'
      + 'This cannot be undone.'
    )) return
    try {
      await api.resetSession()
      onInfo('Session cleared.')
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error))
    }
  }

  const locked = state.running

  return (
    <div className="col" style={{ gap: 18, maxWidth: 1180, paddingBottom: 8 }}>
      {locked && (
        <Notice tone="info">
          The bot is running. Risk limits and refresh rates apply immediately;
          changes to the target wallet, account type or paper mode need a restart.
        </Notice>
      )}

      {live && (
        <Notice tone="danger">
          <strong>Live trading is armed.</strong> Dry-run is off and paper mode is
          off, so the bot will place real orders with real funds as soon as it is
          started.
        </Notice>
      )}

      {/* ---------------- copy trading ---------------- */}
      <section className="panel">
        <div className="panel-body">
          <div className="section-title">Copy trading</div>
          <div className="form-grid">
            <Field
              label="Target wallet to follow"
              hint="The public 0x… address whose trades you want to mirror."
            >
              <input
                className="input mono"
                placeholder="0x…"
                value={String(value('target_wallet') ?? '')}
                onChange={(e) => set('target_wallet', e.target.value.trim())}
              />
            </Field>

            <Field
              label="Trading mode"
              hint={value('trading_mode') === 'fixed_50'
                ? 'One position at a time, each using 50% of your available balance — profits compound automatically.'
                : 'Mirrors the same share of your bankroll that the target used of theirs; several positions can be open at once.'}
            >
              <select
                className="select"
                value={String(value('trading_mode'))}
                onChange={(e) => set('trading_mode', e.target.value)}
              >
                <option value="fixed_50">Fixed 50% (one position, compounding)</option>
                <option value="proportional">Proportional copy</option>
              </select>
            </Field>

            <Field
              label="Target poll interval (seconds)"
              hint="How often the followed wallet's activity feed is checked for new trades. Prices update continuously regardless."
            >
              <input
                className="input mono"
                type="number" min={5} max={3600}
                value={Number(value('poll_interval_seconds'))}
                onChange={(e) => set('poll_interval_seconds', Number(e.target.value))}
              />
            </Field>

            <Field
              label="Minimum $ per trade"
              hint="Smallest position size. A proportional trade smaller than this is bumped up — as long as it stays within the target's size and your balance."
            >
              <input
                className="input mono"
                type="number" min={0.1} step={0.1}
                value={Number(value('min_order_usdc'))}
                onChange={(e) => set('min_order_usdc', Number(e.target.value))}
              />
            </Field>

            <Field
              label="Max expiry (days, 0 = no limit)"
              hint="Ignore trades that settle further out than this, so capital keeps recycling."
            >
              <input
                className="input mono"
                type="number" min={0} max={365}
                value={Number(value('max_expiry_days'))}
                onChange={(e) => set('max_expiry_days', Number(e.target.value))}
              />
            </Field>

            <Field
              label="Minimum seconds to expiry"
              hint="Never enter a market this close to settling, so every copied position opens with a real countdown."
            >
              <input
                className="input mono"
                type="number" min={0}
                value={Number(value('min_seconds_to_expiry'))}
                onChange={(e) => set('min_seconds_to_expiry', Number(e.target.value))}
              />
            </Field>
          </div>

          <div className="form-grid" style={{ marginTop: 14 }}>
            <Check
              checked={Boolean(value('copy_exits'))}
              onChange={(v) => set('copy_exits', v)}
              title="Copy the target's exits"
              hint="When they sell a position you are copying, close your copy too instead of holding to settlement."
            />
          </div>
        </div>
      </section>

      {/* ---------------- mode & safety ---------------- */}
      <section className="panel">
        <div className="panel-body">
          <div className="section-title">Trading mode &amp; safety</div>
          <div className="form-grid">
            <Check
              checked={paper}
              onChange={(v) => {
                set('paper_trading', v)
                if (v) set('dry_run', false)
              }}
              title="Paper trading"
              hint="Fully simulated with a virtual balance against live public prices. No key, no funds, no real orders."
            />
            <Check
              checked={Boolean(value('dry_run'))}
              onChange={(v) => set('dry_run', v)}
              disabled={paper}
              title="Dry-run"
              hint="Uses your real wallet and real prices but places no orders — it logs exactly what it would have done."
            />
            <Check
              checked={Boolean(value('restore_previous_session'))}
              onChange={(v) => set('restore_previous_session', v)}
              title="Restore previous session"
              hint="Off (recommended): each new paper session starts from a clean dashboard. On: previous simulated trades and P/L are kept."
            />
          </div>

          {paper && (
            <div className="form-grid" style={{ marginTop: 14 }}>
              <Field
                label="Paper starting balance"
                hint="The virtual cash a new paper session begins with."
              >
                <input
                  className="input mono"
                  type="number" min={1} step={10}
                  value={Number(value('paper_starting_balance'))}
                  onChange={(e) => set('paper_starting_balance', Number(e.target.value))}
                />
              </Field>
            </div>
          )}
        </div>
      </section>

      {/* ---------------- wallet ---------------- */}
      <section className="panel">
        <div className="panel-body">
          <div className="section-title">Wallet &amp; credentials</div>

          {settings.secureStorage ? (
            <Notice tone={settings.hasPrivateKey ? 'good' : 'info'}>
              {settings.hasPrivateKey
                ? <>A wallet key is stored in <strong>{settings.storageBackend}</strong>
                  {settings.my_wallet_address
                    ? <> for <span className="mono">{shortAddress(settings.my_wallet_address)}</span></>
                    : null}. It is never sent to this page and no API route can read it back.</>
                : <>No key stored yet. Paper trading works without one; live and
                  dry-run trading need one. It will be saved to
                  <strong> {settings.storageBackend}</strong>, never to a file in
                  this project.</>}
            </Notice>
          ) : (
            <Notice tone="danger">
              No secure credential store is available on this system. Install the
              keyring package (<span className="mono">pip install keyring</span>)
              and restart before saving a private key.
            </Notice>
          )}

          <div className="form-grid" style={{ marginTop: 14 }}>
            <Field
              label="Wallet private key"
              hint="64-character hex from Trust Wallet (or Polymarket → Export Private Key). Write-only: typed here, stored by the OS, never displayed again."
            >
              <input
                className="input mono"
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder={settings.hasPrivateKey
                  ? '•••••••• stored — paste to replace'
                  : '0x…'}
                value={privateKey}
                onChange={(e) => setPrivateKey(e.target.value)}
              />
            </Field>

            <Field
              label="Polymarket address (funder)"
              hint="Only needed if your USDC sits in a Polymarket email/browser wallet. Leave blank for Trust Wallet or MetaMask."
            >
              <input
                className="input mono"
                placeholder="0x… — optional"
                value={String(value('funder_address') ?? '')}
                onChange={(e) => set('funder_address', e.target.value.trim())}
              />
            </Field>
          </div>

          <div className="form-actions">
            <button className="btn" disabled={!privateKey.trim() || saving
              || !settings.secureStorage} onClick={saveKey}>
              Save key
            </button>
            <button className="btn primary" disabled={connecting || locked
              || (!privateKey.trim() && !settings.hasPrivateKey)}
              onClick={connect}>
              {connecting ? 'Detecting…' : 'Connect & auto-detect account'}
            </button>
            {settings.hasPrivateKey && (
              <button className="btn ghost" onClick={removeKey} disabled={locked}>
                Remove stored key
              </button>
            )}
            <span className="tiny faint-text">
              Auto-detect figures out your account type and funder for you.
            </span>
          </div>
        </div>
      </section>

      {/* ---------------- performance ---------------- */}
      <section className="panel">
        <div className="panel-body">
          <div className="section-title">Live data &amp; performance</div>
          <div className="form-grid">
            <Check
              checked={Boolean(value('use_price_stream'))}
              onChange={(v) => set('use_price_stream', v)}
              title="Stream order books over WebSocket"
              hint="Recommended. Prices match Polymarket tick-for-tick. Turn off only to fall back to REST polling while diagnosing a network issue."
            />
            <Field
              label="Dashboard update interval (ms)"
              hint="How often live P/L frames are pushed to this page. 400ms feels instant; raise it on a very slow machine."
            >
              <input
                className="input mono"
                type="number" min={100} max={5000} step={50}
                value={Number(value('push_interval_ms'))}
                onChange={(e) => set('push_interval_ms', Number(e.target.value))}
              />
            </Field>
            <Field
              label="Balance refresh (seconds)"
              hint="How often the wallet balance is re-read from the exchange."
            >
              <input
                className="input mono"
                type="number" min={2} max={300}
                value={Number(value('balance_refresh_seconds'))}
                onChange={(e) => set('balance_refresh_seconds', Number(e.target.value))}
              />
            </Field>
            <Field
              label="Market/settlement check (seconds)"
              hint="How often markets are checked for resolution so realized P/L lands promptly."
            >
              <input
                className="input mono"
                type="number" min={5} max={600}
                value={Number(value('market_refresh_seconds'))}
                onChange={(e) => set('market_refresh_seconds', Number(e.target.value))}
              />
            </Field>
            <Field
              label="Exchange reconcile (seconds)"
              hint="How often open positions and resting orders are compared against what the exchange reports."
            >
              <input
                className="input mono"
                type="number" min={10} max={600}
                value={Number(value('reconcile_seconds'))}
                onChange={(e) => set('reconcile_seconds', Number(e.target.value))}
              />
            </Field>
            <Field
              label="REST price fallback (seconds)"
              hint="Safety net: any book the stream has not refreshed within this window is topped up over REST."
            >
              <input
                className="input mono"
                type="number" min={1} max={60}
                value={Number(value('rest_price_fallback_seconds'))}
                onChange={(e) => set('rest_price_fallback_seconds', Number(e.target.value))}
              />
            </Field>
          </div>
        </div>
      </section>

      {/* ---------------- danger zone ---------------- */}
      <section className="panel">
        <div className="panel-body">
          <div className="section-title">Session data</div>
          <div className="row">
            <span className="tiny dim-text grow">
              Clears every trade, order and history record from the local
              database. Your settings and stored key are not affected.
            </span>
            <button className="btn danger" onClick={resetSession} disabled={locked}>
              Clear all session data
            </button>
          </div>
        </div>
      </section>

      {/* Sticky action bar. The column carries matching bottom padding so the
          last panel can always be scrolled clear of it. */}
      <div className="form-actions" style={{
        position: 'sticky', bottom: 0, background: 'var(--bg)',
        borderTop: '1px solid var(--line)', marginTop: 0, paddingBottom: 12,
      }}>
        <button className="btn primary" disabled={!dirty || saving} onClick={save}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        <button className="btn ghost" disabled={!dirty} onClick={() => setDraft({})}>
          Discard changes
        </button>
        {dirty && <span className="tiny warn-text">Unsaved changes</span>}
      </div>
    </div>
  )
}
