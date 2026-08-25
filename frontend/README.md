# 数衡 BankInsight 前端

响应式 PC/H5 智能问数工作台，包含：

- 智能问数、执行阶段反馈、智能图表和业务解释；
- 年 → 季度 → 月 → 日的期末值时间下钻；
- 查询历史、报告详情、Excel/CSV 导出和登录后限时分享；
- 管理概览、操作审计和分级权限说明。

## 本地运行

后端默认地址为 `http://127.0.0.1:8000`，可复制 `.env.example` 为
`.env.local` 并修改 `NEXT_PUBLIC_API_BASE_URL`。

```powershell
npm install
npm run dev
```

访问 `http://localhost:3000`。

默认登录账号：

- 总行管理层：`analyst` / `analyst123`，21项指标、A～M市全部机构；
- 分支行管理层：`analyst2` / `analyst2123`，21项指标、仅A市农商行；
- 业务条线人员：`analyst3` / `analyst3123`，ZB003～ZB006及ZB018～ZB021、仅A市农商行；
- 风险合规人员：`risk` / `risk123`，ZB013～ZB017、A～M市全部机构；
- 财务人员：`finance` / `finance123`，ZB001～ZB002及ZB007～ZB012、A～M市全部机构；
- 系统管理员：`admin` / `admin123`，无业务数据权限，可管理冻结、告警和审计并查看问题，不展示答案和结果。

完整指标名称和 ORG001～ORG013 机构映射见项目根目录 `README.md`。

部署或共享环境请通过后端环境变量修改默认账号和密码，退出后令牌会立即失效，
可以在登录页重新登录。

## 验证

```powershell
npm test
npm run lint
npx tsc --noEmit
```

生产部署时，前端地址需加入后端允许的 CORS 来源；账号角色由后端登录接口
返回并用于数据权限判断，前端不自行提升权限。
