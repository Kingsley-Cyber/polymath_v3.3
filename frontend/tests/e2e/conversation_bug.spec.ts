import { test, expect } from '@playwright/test';

test.describe('Conversation Selector and Delete Bug Investigation', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to the app
    await page.goto('http://localhost:3000');

    // Wait for app to load
    await page.waitForSelector('[data-testid="login-view"]');

    // Login if needed (check if login is required)
    const loginView = page.locator('[data-testid="login-view"]');
    if (await loginView.isVisible()) {
      // Fill in default credentials
      await page.fill('input[placeholder="user@"]', 'admin');
      await page.fill('input[placeholder="pass:"]', 'admin');
      await page.click('button:has-text("EXECUTE LOGIN")');

      // Wait for login to complete
      await page.waitForSelector('[data-testid="chat-window"]', { timeout: 10000 });
    }
  });

  test('Examine conversation selector UI', async ({ page }) => {
    // Look for conversation selector in sidebar
    const sidebar = page.locator('[data-testid="sidebar"]');
    await expect(sidebar).toBeVisible();

    // Look for conversation list or selector
    const conversationList = page.locator('[data-testid="conversation-list"], .conversation-list, [class*="conversation"]');

    if (await conversationList.count() > 0) {
      console.log('Found conversation list with', await conversationList.count(), 'elements');

      // Take screenshot of conversation selector area
      await page.screenshot({
        path: 'conversation-selector.png',
        fullPage: false,
        clip: await conversationList.first().boundingBox()
      });

      // Check for any delete buttons or icons
      const deleteButtons = page.locator('button:has-text("Delete"), [class*="delete"], [class*="trash"]');
      console.log('Delete buttons found:', await deleteButtons.count());

      // Click on first conversation if available
      const firstConversation = conversationList.first();
      await firstConversation.click();

      // Check if delete UI appears or state changes
      await page.waitForTimeout(1000);

      // Look for delete confirmation or related UI
      const deleteConfirm = page.locator('[data-testid="delete-confirm"], .delete-confirm, [class*="confirm"]');
      console.log('Delete confirmation UI found:', await deleteConfirm.count());
    } else {
      console.log('No conversation list found, checking for alternative selectors');

      // Look for any dropdowns that might be conversation selectors
      const dropdowns = page.locator('select, [role="combobox"], [class*="dropdown"]');
      console.log('Dropdowns found:', await dropdowns.count());

      // Take screenshot of sidebar area
      await page.screenshot({
        path: 'sidebar-area.png',
        fullPage: false,
        clip: await sidebar.boundingBox()
      });
    }
  });

  test('Test conversation delete flow', async ({ page }) => {
    // First, create a test conversation if needed
    const chatInput = page.locator('[data-testid="chat-input"], textarea, input[type="text"]');
    await expect(chatInput).toBeVisible();

    // Send a test message to create conversation
    await chatInput.fill('Test message for conversation bug investigation');
    await chatInput.press('Enter');

    // Wait for response or conversation creation
    await page.waitForTimeout(2000);

    // Now look for conversation in sidebar
    const conversationItems = page.locator('[data-testid*="conversation"], [class*="conversation-item"]');

    if (await conversationItems.count() > 0) {
      const firstConversation = conversationItems.first();

      // Hover over conversation to see if delete button appears
      await firstConversation.hover();
      await page.waitForTimeout(500);

      // Look for delete button that appears on hover
      const deleteButton = firstConversation.locator('button:has-text("Delete"), [class*="delete"], [class*="trash"]');

      if (await deleteButton.count() > 0) {
        console.log('Delete button found on hover');

        // Click delete button
        await deleteButton.click();

        // Check for confirmation dialog
        await page.waitForTimeout(1000);

        const confirmDialog = page.locator('[role="dialog"], .modal, [class*="confirm"]');
        console.log('Confirmation dialogs after delete click:', await confirmDialog.count());

        // Take screenshot of confirmation UI
        if (await confirmDialog.count() > 0) {
          await page.screenshot({
            path: 'delete-confirmation.png',
            fullPage: false,
            clip: await confirmDialog.first().boundingBox()
          });
        }
      } else {
        console.log('No delete button found on hover');

        // Try right-click context menu
        await firstConversation.click({ button: 'right' });
        await page.waitForTimeout(500);

        const contextMenu = page.locator('[role="menu"], .context-menu');
        console.log('Context menus after right-click:', await contextMenu.count());
      }
    }
  });

  test('Check conversation selector relationship with delete', async ({ page }) => {
    // This test examines the relationship between selection and delete functionality

    // Look for selected state in conversations
    const conversationItems = page.locator('[data-testid*="conversation"], [class*="conversation-item"]');

    if (await conversationItems.count() > 0) {
      // Check initial selected state
      const selectedConversations = page.locator('[aria-selected="true"], [class*="selected"], [class*="active"]');
      console.log('Initially selected conversations:', await selectedConversations.count());

      // Click first conversation
      const firstConv = conversationItems.first();
      await firstConv.click();
      await page.waitForTimeout(500);

      // Check if selection changed
      const afterClickSelected = page.locator('[aria-selected="true"], [class*="selected"], [class*="active"]');
      console.log('Selected conversations after click:', await afterClickSelected.count());

      // Now try to delete - check if delete is disabled when nothing selected
      const deleteButtons = page.locator('button:has-text("Delete"):not(:disabled)');
      console.log('Enabled delete buttons:', await deleteButtons.count());

      // Try clicking delete if available
      if (await deleteButtons.count() > 0) {
        await deleteButtons.first().click();
        await page.waitForTimeout(1000);

        // Check what happens
        const remainingConversations = page.locator('[data-testid*="conversation"], [class*="conversation-item"]');
        console.log('Conversations after delete attempt:', await remainingConversations.count());
      }
    }
  });

  test('Examine modal sizes and fonts', async ({ page }) => {
    // Open Settings modal
    const settingsButton = page.locator('[data-testid="settings-button"], button:has-text("Settings"), [class*="settings"]');

    if (await settingsButton.count() > 0) {
      await settingsButton.first().click();
      await page.waitForTimeout(1000);

      // Find Settings modal
      const settingsModal = page.locator('[data-testid="settings-modal"], .settings-modal, [class*="modal"]:has-text("Settings")');

      if (await settingsModal.count() > 0) {
        // Get dimensions
        const settingsBox = await settingsModal.first().boundingBox();
        console.log('Settings modal dimensions:', settingsBox);

        // Get font sizes
        const header = settingsModal.locator('h1, h2, h3, .modal-header');
        if (await header.count() > 0) {
          const headerFont = await header.first().evaluate(el => {
            const style = window.getComputedStyle(el);
            return {
              fontSize: style.fontSize,
              fontFamily: style.fontFamily,
              fontWeight: style.fontWeight
            };
          });
          console.log('Settings modal header font:', headerFont);
        }

        // Take screenshot
        await page.screenshot({
          path: 'settings-modal.png',
          fullPage: false,
          clip: settingsBox
        });

        // Close settings modal
        const closeButton = settingsModal.locator('button:has-text("Close"), [class*="close"], button:has(svg)');
        if (await closeButton.count() > 0) {
          await closeButton.first().click();
        } else {
          // Click outside
          await page.mouse.click(10, 10);
        }

        await page.waitForTimeout(500);
      }
    }

    // Open Corpus Manager modal
    const corpusButton = page.locator('[data-testid="corpus-button"], button:has-text("Database"), [class*="database"]');

    if (await corpusButton.count() > 0) {
      await corpusButton.first().click();
      await page.waitForTimeout(1000);

      // Find Corpus Manager modal
      const corpusModal = page.locator('[data-testid="corpus-modal"], .corpus-modal, [class*="modal"]:has-text("Corpus")');

      if (await corpusModal.count() > 0) {
        // Get dimensions
        const corpusBox = await corpusModal.first().boundingBox();
        console.log('Corpus modal dimensions:', corpusBox);

        // Get font sizes
        const header = corpusModal.locator('h1, h2, h3, .modal-header');
        if (await header.count() > 0) {
          const headerFont = await header.first().evaluate(el => {
            const style = window.getComputedStyle(el);
            return {
              fontSize: style.fontSize,
              fontFamily: style.fontFamily,
              fontWeight: style.fontWeight
            };
          });
          console.log('Corpus modal header font:', headerFont);
        }

        // Take screenshot
        await page.screenshot({
          path: 'corpus-modal.png',
          fullPage: false,
          clip: corpusBox
        });

        // Compare with Settings modal
        if (settingsBox && corpusBox) {
          console.log('Width comparison - Settings:', settingsBox.width, 'Corpus:', corpusBox.width);
          console.log('Height comparison - Settings:', settingsBox.height, 'Corpus:', corpusBox.height);
        }
      }
    }
  });
});
