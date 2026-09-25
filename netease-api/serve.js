/**
 * 网易云 API 服务启动器。
 *
 * 直接跑 `node app.js` 时，原版会在启动阶段做一次版本检查（exec 调 npm），
 * 这在受限环境下会因 spawn EPERM 直接崩掉。这里改成：
 *   1) 只做一次匿名 token 注册（失败也不阻塞，只打警告）
 *   2) 用 checkVersion: false 启动 HTTP 服务
 *
 * 用法：
 *   node serve.js                 监听 3000
 *   $env:PORT=4000; node serve.js 换端口
 */
const path = require('path')

const PKG = '@neteasecloudmusicapienhanced/api'
const pkgDir = path.dirname(require.resolve(PKG + '/package.json'))

async function main() {
  const port = Number(process.env.PORT || process.env.NCM_PORT || 3000)

  // 匿名 token 只是让未登录的接口表现更稳定，拿不到也不影响服务启动
  try {
    const generateConfig = require(path.join(pkgDir, 'generateConfig.js'))
    await generateConfig()
  } catch (err) {
    console.warn('[warn] 匿名 token 初始化失败（不影响使用）:', err && err.message ? err.message : err)
  }

  const { serveNcmApi } = require(path.join(pkgDir, 'server.js'))
  serveNcmApi({ port, checkVersion: false })
}

main().catch((err) => {
  console.error('[fatal] 服务启动失败:', err)
  process.exit(1)
})
