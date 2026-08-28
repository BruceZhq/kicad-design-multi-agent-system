import { AccountMenu } from "@/components/account-menu";
import { EvaluationWorkbench } from "@/components/evaluation-workbench";

export const dynamic = "force-dynamic";

export default function EvaluationPage() {
  const enabled = process.env.RATSNEST_EVAL_MODE_ENABLED === "true";

  return (
    <main className="evaluation-page">
      <header className="product-header">
        <a className="wordmark" href="/#workspace" aria-label="返回 KiCad Design Multi-Agent System">
          <span className="wordmark-symbol">KDMAS</span>
          <span>KiCad Design Multi-Agent System</span>
        </a>
        <div className="header-center">配对评测控制台</div>
        <AccountMenu />
      </header>

      {enabled ? (
        <EvaluationWorkbench />
      ) : (
        <section className="evaluation-disabled" aria-labelledby="evaluation-disabled-title">
          <p className="section-kicker">EVALUATION MODE</p>
          <h1 id="evaluation-disabled-title">评测规划页未启用</h1>
          <p>
            生产环境默认关闭。仅在受控评测环境中将服务端环境变量
            <code>RATSNEST_EVAL_MODE_ENABLED</code>设为<code>true</code>后再打开此页。
          </p>
          <a href="/#workspace">返回工程工作区</a>
        </section>
      )}
    </main>
  );
}
