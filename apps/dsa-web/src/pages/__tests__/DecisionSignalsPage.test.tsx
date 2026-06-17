import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import type { DecisionSignalItem, DecisionSignalListResponse } from '../../types/decisionSignals';
import DecisionSignalsPage from '../DecisionSignalsPage';

vi.mock('../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    list: vi.fn(),
    getLatest: vi.fn(),
    updateStatus: vi.fn(),
  },
}));

const signal: DecisionSignalItem = {
  id: 7,
  stockCode: '600519',
  stockName: '贵州茅台',
  market: 'cn',
  sourceType: 'analysis',
  sourceReportId: 3001,
  marketPhase: 'intraday',
  triggerSource: 'web',
  action: 'hold',
  actionLabel: null,
  confidence: 0.72,
  score: 82,
  horizon: '3d',
  entryLow: 1600,
  entryHigh: 1620,
  stopLoss: 1550,
  targetPrice: 1700,
  invalidation: '跌破 1550',
  watchConditions: '观察成交量',
  reason: '趋势保持',
  riskSummary: '放量下跌风险',
  catalystSummary: '业绩窗口',
  evidence: { technical: 'ma' },
  dataQualitySummary: { freshness: 'ok' },
  planQuality: 'complete',
  status: 'active',
  expiresAt: '2026-06-18T09:30:00',
  createdAt: '2026-06-17T09:30:00',
  updatedAt: '2026-06-17T09:30:00',
  metadata: { source: 'test' },
};

function makeSignal(overrides: Partial<DecisionSignalItem> = {}): DecisionSignalItem {
  return {
    ...signal,
    ...overrides,
  };
}

const formattedCreatedAt = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
}).format(new Date('2026-06-17T09:30:00Z'));

function listResponse(items: DecisionSignalItem[] = [signal], total = items.length): DecisionSignalListResponse {
  return {
    items,
    total,
    page: 1,
    pageSize: 20,
  };
}

function renderPage() {
  return render(
    <UiLanguageProvider>
      <DecisionSignalsPage />
    </UiLanguageProvider>,
  );
}

function deferredPromise<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  vi.mocked(decisionSignalsApi.list).mockResolvedValue(listResponse());
  vi.mocked(decisionSignalsApi.getLatest).mockResolvedValue(listResponse([signal]));
  vi.mocked(decisionSignalsApi.updateStatus).mockResolvedValue({ ...signal, status: 'invalidated' });
});

describe('DecisionSignalsPage', () => {
  it('loads active signals by default', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: 'AI 建议' })).toBeInTheDocument();
    await waitFor(() => {
      expect(decisionSignalsApi.list).toHaveBeenCalledWith(expect.objectContaining({
        status: 'active',
        page: 1,
        pageSize: 20,
      }));
    });
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看 贵州茅台 AI 建议详情' })).toBeInTheDocument();
    expect(screen.getByText('贵州茅台').closest('button')).toBeNull();
    expect(screen.getByText('放量下跌风险')).toBeInTheDocument();
    expect(screen.getByText(formattedCreatedAt)).toBeInTheDocument();
  });

  it('passes filter parameters when applying filters', async () => {
    renderPage();
    await screen.findByText('贵州茅台');

    fireEvent.change(screen.getByLabelText('市场'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: '600519' } });
    fireEvent.change(screen.getByLabelText('动作'), { target: { value: 'hold' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    await waitFor(() => {
      expect(decisionSignalsApi.list).toHaveBeenLastCalledWith(expect.objectContaining({
        market: 'cn',
        stockCode: '600519',
        action: 'hold',
        status: 'active',
        page: 1,
        pageSize: 20,
      }));
    });
  });

  it('queries latest active signals by stock code', async () => {
    renderPage();
    await screen.findByText('贵州茅台');

    fireEvent.change(screen.getByLabelText('最新股票代码'), {
      target: { value: '600519' },
    });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    await waitFor(() => {
      expect(decisionSignalsApi.getLatest).toHaveBeenCalledWith('600519', {
        market: undefined,
        limit: 5,
      });
    });
  });

  it('uses the applied market filter for latest lookup instead of draft filter state', async () => {
    renderPage();
    await screen.findByText('贵州茅台');

    const marketSelect = screen.getByLabelText('市场');
    fireEvent.change(marketSelect, { target: { value: 'cn' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));
    await waitFor(() => {
      expect(decisionSignalsApi.list).toHaveBeenLastCalledWith(expect.objectContaining({
        market: 'cn',
      }));
    });

    fireEvent.change(marketSelect, { target: { value: 'hk' } });
    fireEvent.change(screen.getByLabelText('最新股票代码'), {
      target: { value: '600519' },
    });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    await waitFor(() => {
      expect(decisionSignalsApi.getLatest).toHaveBeenCalledWith('600519', {
        market: 'cn',
        limit: 5,
      });
    });
  });

  it('ignores stale latest-search responses', async () => {
    const firstSearch = deferredPromise<DecisionSignalListResponse>();
    const secondSignal = {
      ...signal,
      id: 8,
      stockCode: 'AAPL',
      stockName: 'Apple',
      market: 'us' as const,
      riskSummary: '第二次查询结果',
    };
    vi.mocked(decisionSignalsApi.getLatest)
      .mockReturnValueOnce(firstSearch.promise)
      .mockResolvedValueOnce(listResponse([secondSignal]));
    renderPage();
    await screen.findByText('贵州茅台');

    const latestInput = screen.getByLabelText('最新股票代码');
    fireEvent.change(latestInput, {
      target: { value: '600519' },
    });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    fireEvent.change(latestInput, {
      target: { value: 'AAPL' },
    });
    fireEvent.submit(latestInput.closest('form') as HTMLFormElement);

    expect(await screen.findByText('第二次查询结果')).toBeInTheDocument();

    await act(async () => {
      firstSearch.resolve(listResponse([{ ...signal, riskSummary: '第一次晚返回结果' }]));
      await firstSearch.promise;
    });

    await waitFor(() => {
      expect(screen.queryByText('第一次晚返回结果')).not.toBeInTheDocument();
    });
    expect(screen.getByText('第二次查询结果')).toBeInTheDocument();
  });

  it('renders latest empty and error states', async () => {
    vi.mocked(decisionSignalsApi.getLatest).mockResolvedValueOnce(listResponse([], 0));
    renderPage();
    await screen.findByText('贵州茅台');

    fireEvent.change(screen.getByLabelText('最新股票代码'), {
      target: { value: '600519' },
    });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    expect(await screen.findByText('暂无最新有效信号')).toBeInTheDocument();

    vi.mocked(decisionSignalsApi.getLatest).mockRejectedValueOnce(new Error('latest down'));
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('latest down');
  });

  it('renders empty and error states', async () => {
    vi.mocked(decisionSignalsApi.list).mockResolvedValueOnce(listResponse([], 0));

    renderPage();

    expect(await screen.findByText('暂无决策信号')).toBeInTheDocument();
    vi.mocked(decisionSignalsApi.list).mockRejectedValueOnce(new Error('boom'));
    fireEvent.click(screen.getByRole('button', { name: '刷新' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('boom');
  });

  it('clears stale list data and closes a list drawer when refresh fails', async () => {
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce(listResponse())
      .mockRejectedValueOnce(new Error('filter failed'));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('filter failed');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '查看 贵州茅台 AI 建议详情' })).not.toBeInTheDocument();
    expect(screen.getByText('共 0 条信号')).toBeInTheDocument();
  });

  it('opens details and confirms terminal status updates', async () => {
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce(listResponse())
      .mockResolvedValueOnce(listResponse([], 0));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');
    expect(screen.getAllByText('贵州茅台')).toHaveLength(2);
    expect(within(dialog).getByText('趋势保持')).toBeInTheDocument();
    expect(within(dialog).getByText('#3001')).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: '标记失效' }));
    expect(await screen.findByRole('heading', { name: '更新信号状态' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确定' }));

    await waitFor(() => {
      expect(decisionSignalsApi.updateStatus).toHaveBeenCalledWith(7, { status: 'invalidated' });
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('共 0 条信号')).toBeInTheDocument();
    expect(screen.getByText('暂无决策信号')).toBeInTheDocument();
  });

  it('closes a list-sourced drawer when filters remove the selected signal', async () => {
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce(listResponse())
      .mockResolvedValueOnce(listResponse([], 0));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('暂无决策信号')).toBeInTheDocument();
  });

  it('keeps a latest-sourced drawer open when the main list refreshes', async () => {
    const latestSignal = makeSignal({
      id: 8,
      stockCode: 'AAPL',
      stockName: 'Apple',
      market: 'us',
      riskSummary: 'Latest risk',
    });
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce(listResponse())
      .mockResolvedValueOnce(listResponse([], 0));
    vi.mocked(decisionSignalsApi.getLatest).mockResolvedValueOnce(listResponse([latestSignal]));
    renderPage();

    await screen.findByText('贵州茅台');
    fireEvent.change(screen.getByLabelText('最新股票代码'), { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看 Apple AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Latest risk')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('股票代码'), { target: { value: '600519' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    await waitFor(() => {
      expect(within(screen.getByRole('dialog')).getByText('Latest risk')).toBeInTheDocument();
    });
  });

  it('closes a latest-sourced drawer when the next latest search excludes the selected signal', async () => {
    const firstLatestSignal = makeSignal({
      id: 8,
      stockCode: 'AAPL',
      stockName: 'Apple',
      market: 'us',
      riskSummary: 'Latest A risk',
    });
    const nextLatestSignal = makeSignal({
      id: 9,
      stockCode: 'MSFT',
      stockName: 'Microsoft',
      market: 'us',
      riskSummary: 'Latest B risk',
    });
    vi.mocked(decisionSignalsApi.getLatest)
      .mockResolvedValueOnce(listResponse([firstLatestSignal]))
      .mockResolvedValueOnce(listResponse([nextLatestSignal]));
    renderPage();

    await screen.findByText('贵州茅台');
    const latestInput = screen.getByLabelText('最新股票代码');
    fireEvent.change(latestInput, { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看 Apple AI 建议详情' }));
    expect(within(await screen.findByRole('dialog')).getByText('Latest A risk')).toBeInTheDocument();

    fireEvent.change(latestInput, { target: { value: 'MSFT' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    expect(await screen.findByText('Latest B risk')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('closes a latest-sourced drawer when latest search fails', async () => {
    const latestSignal = makeSignal({
      id: 8,
      stockCode: 'AAPL',
      stockName: 'Apple',
      market: 'us',
      riskSummary: 'Latest risk before failure',
    });
    vi.mocked(decisionSignalsApi.getLatest)
      .mockResolvedValueOnce(listResponse([latestSignal]))
      .mockRejectedValueOnce(new Error('latest failed'));
    renderPage();

    await screen.findByText('贵州茅台');
    const latestInput = screen.getByLabelText('最新股票代码');
    fireEvent.change(latestInput, { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看 Apple AI 建议详情' }));
    expect(within(await screen.findByRole('dialog')).getByText('Latest risk before failure')).toBeInTheDocument();

    fireEvent.change(latestInput, { target: { value: 'MSFT' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('latest failed');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('keeps a list-sourced drawer open when latest search results change', async () => {
    const latestSignal = makeSignal({
      id: 8,
      stockCode: 'AAPL',
      stockName: 'Apple',
      market: 'us',
      riskSummary: 'Latest lookup risk',
    });
    vi.mocked(decisionSignalsApi.getLatest).mockResolvedValueOnce(listResponse([latestSignal]));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    expect(within(await screen.findByRole('dialog')).getByText('趋势保持')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('最新股票代码'), { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '查询最新' }));

    expect(await screen.findByText('Latest lookup risk')).toBeInTheDocument();
    expect(within(screen.getByRole('dialog')).getByText('趋势保持')).toBeInTheDocument();
  });

  it('ignores duplicate status confirmation clicks and disables confirmation controls', async () => {
    const statusUpdate = deferredPromise<DecisionSignalItem>();
    vi.mocked(decisionSignalsApi.updateStatus).mockReturnValueOnce(statusUpdate.promise);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '标记失效' }));
    const confirmButton = await screen.findByRole('button', { name: '确定' });

    fireEvent.click(confirmButton);
    fireEvent.click(confirmButton);

    expect(decisionSignalsApi.updateStatus).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(confirmButton).toBeDisabled());

    await act(async () => {
      statusUpdate.resolve({ ...signal, status: 'invalidated' });
      await statusUpdate.promise;
    });
  });

  it('clamps to a valid page after status update removes the only item on the last page', async () => {
    const pageTwoSignal = makeSignal({ id: 8, stockCode: 'AAPL', stockName: 'Apple', market: 'us' });
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce(listResponse([signal], 21))
      .mockResolvedValueOnce(listResponse([pageTwoSignal], 21))
      .mockResolvedValueOnce(listResponse([], 20))
      .mockResolvedValueOnce(listResponse([signal], 20));
    vi.mocked(decisionSignalsApi.updateStatus).mockResolvedValueOnce({ ...pageTwoSignal, status: 'invalidated' });
    renderPage();

    await screen.findByText('贵州茅台');
    fireEvent.click(screen.getByRole('button', { name: '2' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看 Apple AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '标记失效' }));
    fireEvent.click(await screen.findByRole('button', { name: '确定' }));

    await waitFor(() => {
      expect(decisionSignalsApi.list).toHaveBeenLastCalledWith(expect.objectContaining({
        page: 1,
        pageSize: 20,
      }));
    });
    expect(screen.getByText('共 20 条信号')).toBeInTheDocument();
    expect(screen.queryByText('暂无决策信号')).not.toBeInTheDocument();
  });

  it('closes the status confirmation dialog and shows an error when status update fails', async () => {
    vi.mocked(decisionSignalsApi.updateStatus).mockRejectedValueOnce(new Error('status update failed'));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '标记失效' }));
    expect(await screen.findByRole('heading', { name: '更新信号状态' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '确定' }));

    const errorMessage = await screen.findByText('status update failed');
    expect(errorMessage.closest('[role="alert"]')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole('heading', { name: '更新信号状态' })).not.toBeInTheDocument();
    });
    expect(within(dialog).getByText('有效')).toBeInTheDocument();
  });

  it.each([
    ['关闭信号', 'closed'],
    ['归档', 'archived'],
  ] as const)('confirms %s without exposing active recovery', async (buttonName, status) => {
    vi.mocked(decisionSignalsApi.updateStatus).mockResolvedValueOnce({ ...signal, status });
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: '查看 贵州茅台 AI 建议详情' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByRole('button', { name: '关闭信号' })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '标记失效' })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '归档' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: '有效' })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: '已过期' })).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: buttonName }));
    fireEvent.click(await screen.findByRole('button', { name: '确定' }));

    await waitFor(() => {
      expect(decisionSignalsApi.updateStatus).toHaveBeenCalledWith(7, { status });
    });
  });
});
