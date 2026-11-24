import { useEffect, useState } from 'react';

import { request } from './api';
import { useResource } from './useResource';
import { useUnsavedChanges } from './useUnsavedChanges';
import type { Theme } from './theme';

type InferenceReplica = { name: string; ready: boolean };

type SystemInfo = {
  version: string;
  ollama_available: boolean;
  ollama_models: string[];
  inference_engine?: 'ollama' | 'vllm';
  inference_available?: boolean;
  inference_models?: string[];
  inference_replicas?: InferenceReplica[];
  profiles: { name: string; model: string; engine?: 'ollama' | 'vllm'; reasoning: boolean; prompt_version: string; default: boolean; available?: boolean | null; used?: boolean }[];
  profile_note?: string | null;
  templates: { name: string; version: string }[];
  queue: Partial<Record<'queued' | 'running' | 'failed' | 'paused', number>>;
};

type OllamaModel = {
  name: string;
  parameter_size: string | null;
  quantization: string | null;
  thinking: boolean;
};
type ActiveModel = { profile: string; model: string; reasoning: boolean; chosen: boolean };
export type ModelState = {
  current: ActiveModel;
  default: ActiveModel;
  ollama_available: boolean;
  inference_engine?: 'ollama' | 'vllm';
  inference_available?: boolean;
  models: OllamaModel[];
};

const themes: [Theme, string][] = [['system', 'Как в системе'], ['light', 'Светлая'], ['dark', 'Тёмная']];
const json = { 'Content-Type': 'application/json' };

function describe(model: ActiveModel) {
  return `${model.model}, ${model.reasoning ? 'с рассуждением' : 'без рассуждения'}`;
}

/** Модель для сводок и режим рассуждения; галочка доступна, только если модель умеет рассуждать. */
function ModelCard({ readiness, replicas }: { readiness?: boolean; replicas?: InferenceReplica[] }) {
  const [state, setState] = useState<ModelState>();
  const [model, setModel] = useState('');
  const [reasoning, setReasoning] = useState(false);
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  useUnsavedChanges(!!state && (model !== state.current.model || reasoning !== state.current.reasoning));

  function show(value: ModelState) {
    setState(value);
    setModel(value.current.model);
    setReasoning(value.current.reasoning);
  }

  useEffect(() => {
    const controller = new AbortController();
    void request('/api/v1/model', { signal: controller.signal })
      .then(async (response) => { const value = await response.json(); if (!controller.signal.aborted) show(value); })
      .catch((reason: Error) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, []);

  async function save(body: { model: string | null; reasoning: boolean }, done: string) {
    setBusy('Сохранение…');
    setError('');
    setStatus('');
    try {
      const response = await request('/api/v1/model', { method: 'PUT', headers: json, body: JSON.stringify(body) });
      show(await response.json());
      setStatus(done);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy('');
    }
  }

  if (!state) return error ? <div className="card"><p role="alert">{error}</p></div> : null;
  if (state.inference_engine === 'vllm') {
    const ready = readiness ?? state.inference_available;
    return (
      <div className="card">
        <h3 style={{ marginTop: 0 }}>Модель обработки</h3>
        <p><strong>{state.current.model}</strong> <span className="muted">через vLLM</span></p>
        <p role="status">
          <span className={`pill ${ready ? 'ok' : 'warn'}`}>
            {ready ? 'Модель доступна' : 'Модель недоступна'}
          </span>
        </p>
        {replicas?.length ? <ul aria-label="Экземпляры vLLM">
          {replicas.map(({ name, ready: replicaReady }) => (
            <li key={name}>{name} · {replicaReady ? 'готов' : 'недоступен'}</li>
          ))}
        </ul> : null}
        {!ready && <p>Дождитесь загрузки модели. Если статус сохраняется, обратитесь к администратору.</p>}
      </div>
    );
  }

  const selected = state.models.find((item) => item.name === model);
  const canThink = !!selected?.thinking;
  const changed = model !== state.current.model || reasoning !== state.current.reasoning;
  return (
    <div className="card" aria-busy={!!busy}>
      <h3 style={{ marginTop: 0 }}>Модель для сводок</h3>
      <p>
        Сейчас: <b>{describe(state.current)}</b>{' '}
        <span className="muted">
          {state.current.chosen ? '— выбрано здесь' : `— профиль ${state.current.profile} из profiles.yaml`}
        </span>
      </p>
      {state.ollama_available ? (
        <form className="row" onSubmit={(event) => {
          event.preventDefault();
          void save({ model, reasoning: canThink && reasoning }, 'Модель сохранена.');
        }}>
          <label className="field">
            <span>Модель Ollama</span>
            <select
              value={model}
              disabled={!!busy}
              onChange={(event) => {
                const next = state.models.find((item) => item.name === event.target.value);
                setModel(event.target.value);
                if (!next?.thinking) setReasoning(false);
              }}
            >
              {!selected && <option value={model}>{model} — нет в Ollama</option>}
              {state.models.map((item) => (
                <option key={item.name} value={item.name}>
                  {[item.name, item.parameter_size, item.quantization].filter(Boolean).join(' · ')}
                </option>
              ))}
            </select>
          </label>
          <label className="check" style={{ alignSelf: 'end' }}>
            <input
              type="checkbox"
              checked={canThink && reasoning}
              disabled={!!busy || !canThink}
              onChange={(event) => setReasoning(event.target.checked)}
            />
            Режим рассуждения
          </label>
          <button className="primary" disabled={!!busy || !changed || !selected} style={{ alignSelf: 'end' }}>
            Сохранить
          </button>
          {state.current.chosen && (
            <button
              type="button"
              disabled={!!busy}
              style={{ alignSelf: 'end' }}
              onClick={() => void save({ model: null, reasoning: false }, 'Возвращена модель из profiles.yaml.')}
            >
              Как в profiles.yaml
            </button>
          )}
        </form>
      ) : (
        <p className="notice">Ollama недоступна: список моделей не получить, выбор модели отложен.</p>
      )}
      {selected && !canThink && <p className="muted">У этой модели нет режима рассуждения.</p>}
      <p className="muted">
        Новая модель применяется к докладам, которые обрабатываются после сохранения; готовые сводки
        не пересчитываются. По умолчанию — {describe(state.default)} (профиль {state.default.profile}).
      </p>
      <p role="status">{busy || status}</p>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

export function Settings({ theme, onTheme }: { theme: Theme; onTheme: (theme: Theme) => void }) {
  const { data: info, error } = useResource<SystemInfo>('/api/v1/system', 5000);
  const engine = info?.inference_engine === 'vllm' ? 'vLLM' : 'Ollama';
  const inferenceAvailable = info?.inference_available ?? info?.ollama_available;
  const inferenceModels = info?.inference_models ?? info?.ollama_models ?? [];

  return (
    <section aria-labelledby="settings-title">
      <div className="card">
        <h2 id="settings-title">Настройки</h2>
        <fieldset>
          <legend>Тема оформления</legend>
          <div className="row">
            {themes.map(([value, label]) => (
              <label key={value} className="check">
                <input type="radio" name="theme" checked={theme === value} onChange={() => onTheme(value)} />
                {label}
              </label>
            ))}
          </div>
        </fieldset>
        {error && <p role="alert">{error}</p>}
      </div>

      <ModelCard readiness={inferenceAvailable} replicas={info?.inference_replicas} />

      {info && (
        <details className="card system-details"><summary>Состояние сервиса и технические сведения</summary>
          <h3>Установка</h3>
          <div className="kv">
            <div><b>Версия</b>{info.version}</div>
            <div>
              <b>{engine}</b>
              {inferenceAvailable
                ? <span className="pill ok">{engine === 'vLLM' ? 'доступен' : 'доступна'}</span>
                : <span className="pill bad">{engine === 'vLLM' ? 'недоступен' : 'недоступна'}</span>}
            </div>
            <div><b>В очереди</b>{info.queue.queued ?? 0}</div>
            <div><b>Обрабатывается</b>{info.queue.running ?? 0}</div>
            <div><b>С ошибкой</b>{info.queue.failed ?? 0}</div>
            <div><b>Приостановлено</b>{info.queue.paused ?? 0}</div>
          </div>
          {inferenceAvailable && (
            <p className="muted">
              Модели {engine}: {inferenceModels.length ? inferenceModels.join(', ') : 'не загружены'}.
            </p>
          )}
          <h3>Профили моделей</h3>
          {info.profile_note && <p className="notice" role="status">{info.profile_note}</p>}
          {info.profiles.length ? (
            <div className="table-wrap">
              <table>
                <thead><tr><th scope="col">Профиль</th><th scope="col">Модель</th><th scope="col">Рассуждение</th><th scope="col">Промпты</th></tr></thead>
                <tbody>
                  {info.profiles.map((profile) => (
                    <tr key={profile.name}>
                      <th scope="row">
                        {profile.name}{' '}
                        {profile.default && <span className="pill info">по умолчанию</span>}{' '}
                        {profile.used && <span className="pill ok">используется</span>}
                      </th>
                      <td>{profile.model}{profile.available === false && <> <span className="pill warn">{profile.engine === 'vllm' ? 'не обслуживается vLLM' : 'нет в каталоге моделей'}</span></>}</td>
                      <td>{profile.reasoning ? 'да' : 'нет'}</td>
                      <td>{profile.prompt_version}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="muted">Файл профилей недоступен.</p>}
          <p className="muted">
            Профиль по умолчанию задаётся в profiles.yaml; смена профиля перезапускает шаги сводки.
            Доклады с ошибкой повторяются на этапе «Проверка» конференции.
          </p>
          <h3>Бланки справки</h3>
          <ul>{info.templates.map((item) => <li key={item.name}>{item.name} — версия {item.version}</li>)}</ul>
        </details>
      )}
    </section>
  );
}
