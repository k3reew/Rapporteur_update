import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { Processing } from './Processing';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const talk = {
  id: 't1', section_id: 's', section: 'Секция', order: 1, status: 'processed', job: 'done', step: null,
  error: null, summary_number: null, summary_status: null, bulk_reviewable: false,
};

test('оператор выбирает сводку или только карточку', async () => {
  const puts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      puts.push([url, JSON.parse(String(init.body))]);
      return new Response(null, { status: 204 });
    }
    return Response.json({
      processing_started: true,
      files: 2,
      talks: [
        { ...talk, title: 'Кэширование', scope: 'card', card_only: true },
        { ...talk, id: 't2', order: 2, title: 'Сжатие', scope: 'auto', card_only: false, summary_number: 1 },
      ],
    });
  }));
  render(<Processing conferenceId="c" onOpen={() => {}} />);

  expect(await screen.findByText('только карточка')).toBeDefined();
  expect(screen.getByText(/только карточка — у 1/)).toBeDefined();
  const buttons = screen.getAllByRole<HTMLButtonElement>('button', { name: 'Открыть сводку' });
  expect(buttons.map(button => button.disabled)).toEqual([true, false]);

  fireEvent.change(screen.getByLabelText('Результат: Сжатие'), { target: { value: 'card' } });
  await vi.waitFor(() => expect(puts).toEqual([['/api/v1/talks/t2/scope', { scope: 'card' }]]));
});
