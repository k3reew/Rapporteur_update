import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { ReportPanel } from './ReportPanel';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const conference = {
  id: 'conference',
  title: 'Конференция',
  start_date: null,
  end_date: null,
  place: null,
  organizer: null,
  processing_started: true,
  talks: 1,
  summaries: 1,
  reviewed: 0,
};
const progress = {
  processing_started: true,
  files: 1,
  talks: [{ id: 'talk', section: 'Секция', order: 1, title: 'Доклад', status: 'processed', job: 'done', step: null, error: null, summary_number: 1, summary_status: 'ready', screening: { eligible: true }, reportable: true }],
};
const version = {
  id: 'version',
  number: 1,
  created_at: '2026-11-09T10:00:00Z',
  files: [{ format: 'docx' }, { format: 'md' }, { format: 'pdf' }],
  variables: { talks_count: 1 },
  warnings: ['Сводка не проверена человеком: «Доклад»'],
};

test.each([true, false])('предупреждает о датах и собирает справку (randomUUID доступен: %s)', async (hasRandomUUID) => {
  if (!hasRandomUUID) vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
  let submitted = false;
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json(progress);
    if (url.endsWith('/report-jobs')) {
      if (init?.method === 'POST') { submitted = true; return Response.json({ id: 'job' }, { status: 202 }); }
      return Response.json(submitted ? [{ id: 'job', status: 'done', step: 'done', report_version_id: 'version' }] : []);
    }
    if (url.endsWith('/reports')) return Response.json(submitted ? [version] : []);
    return Response.json(conference);
  });
  vi.stubGlobal('fetch', api);
  render(<ReportPanel conferenceId="conference" />);

  await screen.findByText('Без дат в справке не будет периода проведения, сборка не пройдёт.');
  await screen.findByText('Собранных версий пока нет.');
  fireEvent.click(screen.getByLabelText('Собрать, даже если не все сводки проверены'));
  fireEvent.click(screen.getByRole('button', { name: 'Собрать справку' }));

  await screen.findByText('Версия 1');
  expect(screen.getByText('Сводка не проверена человеком: «Доклад»')).toBeDefined();
  expect(screen.getByRole('link', { name: 'PDF' }).getAttribute('href'))
    .toBe('/api/v1/report-versions/version/files/pdf');
  const call = api.mock.calls.find(([, init]) => init?.method === 'POST');
  expect(call?.[0]).toBe('/api/v1/conferences/conference/report-jobs');
  expect(JSON.parse(call?.[1]?.body as string)).toEqual({ unreviewed_confirmed: true, talk_ids: ['talk'], idempotency_key: expect.any(String) });

});

test('собирает только выбранные доклады', async () => {
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json({ ...progress, talks: [...progress.talks, { ...progress.talks[0], id: 'two', title: 'Второй' }] });
    if (url.endsWith('/report-jobs')) return Response.json(init?.method === 'POST' ? { id: 'job' } : []);
    if (url.endsWith('/reports')) return Response.json([]);
    return Response.json(conference);
  });
  vi.stubGlobal('fetch', api);
  render(<ReportPanel conferenceId="conference" />);
  fireEvent.click(await screen.findByRole('checkbox', { name: /Второй/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Собрать справку' }));
  await screen.findByText('Сборка поставлена в очередь. Можно переходить к другим разделам.');
  const body = api.mock.calls.find(([, init]) => init?.method === 'POST')?.[1]?.body;
  expect(JSON.parse(body as string).talk_ids).toEqual(['talk']);
});

test('по HTTP повторяет запрос с тем же ключом, а изменённый состав отправляет с новым', async () => {
  vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) });
  const submitted: { idempotency_key: string; talk_ids: string[] }[] = [];
  const api = async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json({ ...progress, talks: [...progress.talks, { ...progress.talks[0], id: 'two', title: 'Второй' }] });
    if (url.endsWith('/report-jobs')) {
      if (init?.method !== 'POST') return Response.json([]);
      submitted.push(JSON.parse(init.body as string));
      return submitted.length === 1
        ? Response.json({ message: 'Временный сбой' }, { status: 503 })
        : Response.json({ id: 'job' }, { status: 202 });
    }
    if (url.endsWith('/reports')) return Response.json([]);
    return Response.json(conference);
  };
  vi.stubGlobal('fetch', api);
  render(<ReportPanel conferenceId="conference" />);
  const second = await screen.findByRole('checkbox', { name: 'Второй' });
  const build = screen.getByRole('button', { name: 'Собрать справку' });
  fireEvent.click(build);
  await screen.findByRole('alert');
  fireEvent.click(build);
  await screen.findByText('Сборка поставлена в очередь. Можно переходить к другим разделам.');
  expect(submitted[0].idempotency_key.length).toBeGreaterThan(0);
  expect(submitted[1].idempotency_key).toBe(submitted[0].idempotency_key);
  fireEvent.click(second);
  fireEvent.click(build);
  await screen.findByText('Сборка поставлена в очередь. Можно переходить к другим разделам.');
  expect(submitted[2].talk_ids).toEqual(['talk']);
  expect(submitted[2].idempotency_key).not.toBe(submitted[0].idempotency_key);
});

test('восстанавливает неудачное задание после открытия страницы и повторяет его', async () => {
  let failed = true;
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json(progress);
    if (url.endsWith('/reports')) return Response.json([]);
    if (url.endsWith('/retry')) { failed = false; return Response.json({}); }
    if (url.endsWith('/report-jobs')) return Response.json([{ id: 'saved', status: failed ? 'failed' : 'queued', step: failed ? 'failed' : 'queued', error: failed ? 'PDF недоступен' : null }]);
    void init;
    return Response.json(conference);
  });
  vi.stubGlobal('fetch', api);
  render(<ReportPanel conferenceId="conference" />);
  await screen.findByText('PDF недоступен');
  fireEvent.click(screen.getByRole('button', { name: 'Повторить сборку' }));
  await screen.findByText('Справка собирается · В очереди');
  expect(api).toHaveBeenCalledWith('/api/v1/report-jobs/saved/retry', { method: 'POST' });
});

test('доклад без интереса отделов выбирается карточкой, без отбора — нет', async () => {
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/progress')) return Response.json({ ...progress, talks: [
      { ...progress.talks[0], id: 'card', title: 'Карточка', summary_number: null, summary_status: null, card_only: true },
      { ...progress.talks[0], id: 'wait', title: 'Ждёт отбора', reportable: false },
    ] });
    if (url.endsWith('/report-jobs')) return Response.json(init?.method === 'POST' ? { id: 'job' } : []);
    if (url.endsWith('/reports')) return Response.json([]);
    return Response.json(conference);
  });
  vi.stubGlobal('fetch', api);
  render(<ReportPanel conferenceId="conference" />);
  const card = await screen.findByRole<HTMLInputElement>('checkbox', { name: /Карточка/ });
  expect(card.checked).toBe(true);
  expect(screen.getByText('· только карточка')).toBeDefined();
  expect(screen.getByRole<HTMLInputElement>('checkbox', { name: /Ждёт отбора/ }).disabled).toBe(true);
});
