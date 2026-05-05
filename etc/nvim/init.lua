-- SyncListen 专用 nvim 配置：仅 flash.nvim 选词跳转 + 持久化 undo
-- 启用方式：export EDITOR='env NVIM_APPNAME=synclisten-nvim nvim'

-- ── bootstrap lazy.nvim（仅首次启动会拉一次） ────────────────
local lazypath = vim.fn.stdpath("data") .. "/lazy/lazy.nvim"
if not (vim.uv or vim.loop).fs_stat(lazypath) then
  vim.fn.system({
    "git", "clone", "--filter=blob:none", "--branch=stable",
    "https://github.com/folke/lazy.nvim.git", lazypath,
  })
end
vim.opt.rtp:prepend(lazypath)

-- ── 可恢复性：关 swap/backup，开持久 undo ────────────────────
vim.opt.swapfile = false
vim.opt.backup = false
vim.opt.writebackup = false
vim.opt.undofile = true
vim.opt.undodir = vim.fn.stdpath("state") .. "/undo"
vim.opt.undolevels = 10000

-- ── 基础 UI ──────────────────────────────────────────────────
vim.opt.number = true
vim.opt.mouse = "a"
vim.opt.termguicolors = true
vim.opt.wrap = true
vim.opt.linebreak = true
vim.opt.signcolumn = "no"
vim.opt.shortmess:append("I")

vim.g.mapleader = " "
vim.g.maplocalleader = " "

-- ── 插件：仅 flash.nvim ──────────────────────────────────────
require("lazy").setup({
  {
    "folke/flash.nvim",
    opts = {
      -- labels 改为大写字母，避免与小写拼音首字母输入冲突：
      -- 小写 a-z 永远延伸 pattern；按 Shift+字母 才是跳转。
      labels = "ASDFGHJKLQWERTYUIOPZXCVBNM",
    },
    keys = {
      {
        "s",
        mode = { "n", "x", "o" },
        function()
          require("flash").jump({
            search = { mode = require("pyinitial").mode },
          })
        end,
        desc = "Flash jump (拼音首字母)",
      },
      { "S", mode = { "n", "x", "o" }, function() require("flash").treesitter() end, desc = "Flash treesitter" },
      { "<c-s>", mode = { "c" }, function() require("flash").toggle() end, desc = "Toggle flash in / search" },
    },
  },
}, {
  change_detection = { enabled = false, notify = false },
  ui = { border = "rounded" },
})

-- ── 工作流便捷键：保存并返回 SyncListen ──────────────────────
vim.keymap.set("n", "<leader>w", "<cmd>wq<cr>", { desc = "保存并返回" })
