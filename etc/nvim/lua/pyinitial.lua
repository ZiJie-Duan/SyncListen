-- 全拼首字母 matcher：把用户输入的每个 ASCII 字母扩展为
-- "[原字母大小写 + 所有以该字母为拼音首字母的汉字]" 字符类。
-- 配合 flash.nvim 的 search.mode 钩子使用。

local M = {}

local data = nil
local function get_data()
  if data == nil then
    local ok, mod = pcall(require, "pyinitial_data")
    data = ok and mod or {}
  end
  return data
end

local function expand(c)
  local lc = c:lower()
  if not lc:match("^[a-z]$") then
    return c
  end
  local chars = get_data()[lc] or ""
  return "[" .. c:upper() .. lc .. chars .. "]"
end

---@param pattern string
---@return string vim_regex_pattern, string skip_pattern
function M.mode(pattern)
  if pattern == nil or pattern == "" then
    return pattern, pattern
  end
  local out = {}
  for i = 1, #pattern do
    out[#out + 1] = expand(pattern:sub(i, i))
  end
  local p = table.concat(out)
  return p, p
end

return M
