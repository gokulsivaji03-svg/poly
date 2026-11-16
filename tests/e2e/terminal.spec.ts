import { test, expect } from '@playwright/test';

test('authenticated terminal, persisted controls, desktop and mobile', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto('/login');
  await page.getByTestId('login-username').fill(process.env.TERMINAL_USER ?? 'admin');
  await page.getByTestId('login-password').fill(process.env.TERMINAL_PASSWORD ?? 'change-me');
  await page.getByTestId('login-submit').click();
  await expect(page.getByTestId('stat-equity')).toBeVisible();
  await expect(page.getByTestId('conn-status')).toHaveAttribute('data-state', 'connected');
  await page.screenshot({ path: info.outputPath('dashboard-desktop.png'), fullPage: true });

  await page.getByTestId('nav-wallets').click();
  await expect(page.getByTestId('wallet-address-input')).toBeVisible();
  await page.getByTestId('wallet-address-input').fill('invalid');
  await page.getByTestId('wallet-add-submit').click();
  await expect(page.getByTestId('wallet-address-input')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByText('Must be 0x followed by 40 hex characters.')).toBeVisible();
  await page.getByTestId('wallet-address-input').clear();

  await page.getByTestId('nav-positions').click();
  await expect(page.getByRole('heading', { name: 'Positions', exact: true })).toBeVisible();
  await page.getByTestId('nav-hedges').click();
  await expect(page.getByTestId('hedge-mode-select')).toBeVisible();
  await page.getByTestId('nav-activity').click();
  await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
  await page.getByTestId('nav-replay').click();
  await expect(page.getByTestId('replay-start')).toBeVisible();
  await page.getByTestId('replay-submit').click();
  await expect(page.getByText('Select at least one wallet.', { exact: true })).toBeVisible();

  await page.getByTestId('nav-settings').click();
  const multiplier = page.getByTestId('settings-multiplier');
  await expect(multiplier).toBeVisible();
  const original = await multiplier.inputValue();
  const changed = original === '1.25' ? '1.5' : '1.25';
  await multiplier.fill(changed);
  await page.getByTestId('settings-save').click();
  await expect(page.getByTestId('settings-save')).toBeDisabled();
  await page.reload();
  await expect(multiplier).toHaveValue(changed);
  await multiplier.fill(original);
  await page.getByTestId('settings-save').click();
  await expect(page.getByTestId('settings-save')).toBeDisabled();
  await page.getByTestId('btn-pause').click();
  await expect(page.getByTestId('btn-resume')).toBeVisible();
  await page.getByTestId('btn-resume').click();
  await expect(page.getByTestId('btn-pause')).toBeVisible();

  await page.getByTestId('nav-system').click();
  await expect(page.getByRole('heading', { name: /^Polymarket International/ })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByTestId('stat-equity')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('dashboard-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});
