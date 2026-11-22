import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Departments, Recommendations } from './Departments';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
test('правка рекомендации сохраняет исходную версию при обновлении результата', async () => {
  vi.useFakeTimers();
  let version = 1;
  const api = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/departments')) return Response.json([]);
    if (init?.method === 'PUT') return Response.json({ message: 'Конфликт версии' }, { status: 409 });
    return Response.json({ version, items: [{ department_id: 'd', department_version: 1, reason: 'Причина', claim_ids: [], paragraph_ids: [] }], reviewed: false, stale: false });
  });
  vi.stubGlobal('fetch', api);
  render(<Recommendations talkId="t" />);
  await act(async () => {});
  fireEvent.change(screen.getByLabelText('Обоснование'), { target: { value: 'Моя правка' } });
  version = 2;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  fireEvent.click(screen.getByRole('button', { name: 'Подтвердить рекомендации' }));
  await act(async () => {});
  const body = api.mock.calls.find(([, init]) => init?.method === 'PUT')?.[1]?.body;
  expect(JSON.parse(body as string).version).toBe(1);
  expect(screen.getByLabelText<HTMLTextAreaElement>('Обоснование').value).toBe('Моя правка');
});

test('отдел удаляется после подтверждения и пропадает из списка', async () => {
  let deleted = false;
  const api = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') { deleted = true; return new Response(null, { status: 204 }); }
    return Response.json(deleted ? [] : [{ id: 'dep', version: 2, name: 'Отдел хранения', original_name: 'положение.txt', pages: [{ page: 1, text: 'Обязанности' }] }]);
  });
  vi.stubGlobal('fetch', api);
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  render(<Departments />);
  fireEvent.click(await screen.findByText(/Отдел хранения · версия 2/));
  fireEvent.click(screen.getByRole('button', { name: 'Удалить отдел' }));
  await vi.waitFor(() => expect(api).toHaveBeenCalledWith('/api/v1/departments/dep', { method: 'DELETE' }));
  await vi.waitFor(() => expect(screen.queryByText(/Отдел хранения · версия 2/)).toBeNull());
});

test('отдел не удаляется без подтверждения диалога', async () => {
  const api = vi.fn(async (_url: string, _init?: RequestInit) => {
    void _url; void _init;
    return Response.json([{ id: 'dep', version: 2, name: 'Отдел хранения', original_name: 'положение.txt', pages: [] }]);
  });
  vi.stubGlobal('fetch', api);
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<Departments />);
  fireEvent.click(await screen.findByText(/Отдел хранения · версия 2/));
  fireEvent.click(screen.getByRole('button', { name: 'Удалить отдел' }));
  expect(api.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);
});
