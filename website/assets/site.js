document.querySelectorAll('input[type="search"]').forEach(input => {
  const container = input.closest('aside') || document.querySelector('main');
  const items = [...container.querySelectorAll('[data-filter-item]')];
  input.addEventListener('input', () => {
    const query = input.value.toLocaleLowerCase().trim();
    let count = 0;
    items.forEach(item => {
      item.hidden = !item.textContent.toLocaleLowerCase().includes(query);
      if (!item.hidden) count++;
    });
    const output = container.querySelector('#filter-count');
    if (output) output.textContent = `${count} ${count === 1 ? 'file' : 'files'}`;
  });
});
document.querySelectorAll('pre').forEach(pre => {
  if (pre.closest('.source-code')) return;
  const codeText = pre.querySelector('code')?.textContent || pre.textContent;
  const button = document.createElement('button');
  button.className = 'copy';
  button.type = 'button';
  button.textContent = 'Copy';
  button.setAttribute('aria-label', 'Copy code block');
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Select text to copy';
    }
    setTimeout(() => { button.textContent = 'Copy'; }, 2000);
  });
  pre.append(button);
});
