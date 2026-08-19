import { useRef } from "react";
import {
  motion,
  MotionValue,
  useInView,
  useScroll,
  useTransform
} from "framer-motion";
import {
  ArrowRight,
  Check,
  CircuitBoard,
  GitBranch,
  RadioTower,
  Zap
} from "lucide-react";

const primaryText = "#E1E0CC";
const easeOut = [0.16, 1, 0.3, 1] as const;

interface WordsPullUpProps {
  text: string;
  className?: string;
  showAsterisk?: boolean;
  center?: boolean;
}

function WordsPullUp({
  text,
  className = "",
  showAsterisk = false,
  center = false
}: WordsPullUpProps) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-10% 0px" });
  const words = text.split(" ");

  return (
    <span
      ref={ref}
      className={`inline-flex flex-wrap ${center ? "justify-center" : ""} ${className}`}
      aria-label={text}
    >
      {words.map((word, index) => (
        <span className="overflow-hidden pr-[0.08em]" key={`${word}-${index}`}>
          <motion.span
            aria-hidden="true"
            className="relative inline-block"
            initial={{ y: 28, opacity: 0 }}
            animate={isInView ? { y: 0, opacity: 1 } : { y: 28, opacity: 0 }}
            transition={{
              duration: 0.8,
              delay: index * 0.08,
              ease: easeOut
            }}
          >
            {word}
            {showAsterisk && index === words.length - 1 ? (
              <span className="absolute -right-[0.28em] top-[0.58em] text-[0.26em] leading-none">
                *
              </span>
            ) : null}
          </motion.span>
          {index < words.length - 1 ? <span aria-hidden="true">&nbsp;</span> : null}
        </span>
      ))}
    </span>
  );
}

interface Segment {
  text: string;
  className?: string;
}

function WordsPullUpMultiStyle({
  segments,
  className = ""
}: {
  segments: Segment[];
  className?: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-10% 0px" });
  const words = segments.flatMap((segment, segmentIndex) =>
    segment.text.split(" ").map((word, wordIndex) => ({
      word,
      className: segment.className ?? "",
      key: `${segmentIndex}-${wordIndex}-${word}`
    }))
  );

  return (
    <div
      ref={ref}
      className={`inline-flex flex-wrap justify-center ${className}`}
    >
      {words.map((item, index) => (
        <span className="overflow-hidden pr-[0.12em]" key={item.key}>
          <motion.span
            className={`inline-block ${item.className}`}
            initial={{ y: 20, opacity: 0 }}
            animate={isInView ? { y: 0, opacity: 1 } : { y: 20, opacity: 0 }}
            transition={{
              duration: 0.7,
              delay: index * 0.055,
              ease: easeOut
            }}
          >
            {item.word}
          </motion.span>
          {index < words.length - 1 ? <span>&nbsp;</span> : null}
        </span>
      ))}
    </div>
  );
}

function AnimatedLetter({
  char,
  index,
  total,
  progress
}: {
  char: string;
  index: number;
  total: number;
  progress: MotionValue<number>;
}) {
  const start = Math.max(0, index / total - 0.1);
  const end = Math.min(1, index / total + 0.06);
  const opacity = useTransform(progress, [start, end], [0.22, 1]);

  return (
    <motion.span style={{ opacity }}>{char}</motion.span>
  );
}

function ScrollRevealText({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement | null>(null);
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start 0.85", "end 0.25"]
  });
  const words = text.split(" ");
  const totalChars = Array.from(text).length;
  let charOffset = 0;

  return (
    <p
      ref={ref}
      className="mx-auto mt-8 flex max-w-3xl flex-wrap justify-center gap-x-[0.25em] text-center text-xs leading-relaxed text-primary sm:text-sm md:text-base"
    >
      {words.map((word, wordIndex) => {
        const start = charOffset;
        charOffset += word.length + 1;
        return (
          <span className="inline-block" key={`${word}-${wordIndex}`}>
            {Array.from(word).map((char, charIndex) => (
              <AnimatedLetter
                char={char}
                index={start + charIndex}
                key={`${char}-${charIndex}`}
                progress={scrollYProgress}
                total={totalChars}
              />
            ))}
          </span>
        );
      })}
    </p>
  );
}

const features = [
  {
    icon: CircuitBoard,
    number: "01",
    title: "Design Generation.",
    copy: "Natural language becomes a KiCad project path with a verified run record.",
    checks: ["Template and MCP backends", "Typed design specification", "ERC-ready output trail"]
  },
  {
    icon: Zap,
    number: "02",
    title: "Repair Loop.",
    copy: "Findings become patch plans, score deltas, and converge-or-escalate decisions.",
    checks: ["Evaluate findings", "Apply repair mappings", "Reject new critical regressions"]
  },
  {
    icon: RadioTower,
    number: "03",
    title: "ATDP Trajectory.",
    copy: "Every orchestrator step and MCP tool call can be captured as learning signal.",
    checks: ["Node-level event stream", "Reward and outcome traces", "Control plane ingestion"]
  },
  {
    icon: GitBranch,
    number: "04",
    title: "Heuristic Evolution.",
    copy: "Candidate strategies are tested against benchmarks before they can replace incumbents.",
    checks: ["Candidate vs incumbent gates", "Rollback-safe promotion", "Benchmark-backed scoring"]
  }
];

function HealthPill({
  health,
  healthError
}: {
  health: string;
  healthError: string | null;
}) {
  const isOnline = health === "ok";

  return (
    <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-black/45 px-3 py-1.5 text-[11px] text-primary/75 backdrop-blur">
      <span
        className={`h-2 w-2 rounded-full ${isOnline ? "bg-emerald-300" : "bg-amber-300"}`}
      />
      <span>{isOnline ? "control plane online" : healthError ?? "checking API"}</span>
    </div>
  );
}

export function Hero({
  health,
  healthError
}: {
  health: string;
  healthError: string | null;
}) {
  const navItems = ["System", "Agents", "Evolution", "Console", "Runs"];

  return (
    <section className="h-screen bg-black p-4 md:p-6">
      <div className="relative h-full overflow-hidden rounded-2xl bg-[#030303] md:rounded-[2rem]">
        <div className="lab-field absolute inset-0" />
        <div className="noise-overlay absolute inset-0 opacity-[0.72] mix-blend-overlay" />
        <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-transparent to-black/75" />
        <div className="absolute left-1/2 top-0 z-20 -translate-x-1/2 rounded-b-2xl bg-black px-4 py-2 md:rounded-b-3xl md:px-8">
          <nav className="flex items-center gap-3 text-[10px] sm:gap-6 sm:text-xs md:gap-12 md:text-sm lg:gap-14">
            {navItems.map((item) => (
              <a
                href={item === "Console" || item === "Runs" ? "#console" : `#${item.toLowerCase()}`}
                key={item}
                style={{ color: "rgba(225, 224, 204, 0.8)" }}
                className="whitespace-nowrap transition-colors hover:text-[#E1E0CC]"
              >
                {item}
              </a>
            ))}
          </nav>
        </div>

        <div className="absolute left-4 top-24 z-10 sm:left-6 sm:top-16 md:left-8">
          <HealthPill health={health} healthError={healthError} />
        </div>

        <div className="absolute bottom-0 left-0 right-0 z-10 px-4 pb-5 sm:px-6 md:px-8 md:pb-7">
          <div className="grid min-w-0 items-end gap-6 lg:grid-cols-12">
            <div className="min-w-0 lg:col-span-8">
              <h1
                className="max-w-full pr-5 text-[64px] font-medium leading-[0.85] tracking-[0] sm:text-[96px] md:text-[120px] lg:text-[150px] xl:text-[176px] 2xl:text-[196px]"
                style={{ color: primaryText }}
              >
                <WordsPullUp text="RatsNest" showAsterisk />
              </h1>
            </div>
            <div className="min-w-0 max-w-full overflow-hidden pb-2 lg:col-span-4 lg:pb-8">
              <motion.p
                initial={{ y: 20, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ delay: 0.5, duration: 0.8, ease: easeOut }}
                className="w-[calc(100vw-4rem)] max-w-full break-words text-xs leading-[1.25] text-primary/70 sm:w-auto sm:max-w-lg sm:text-sm md:text-base"
              >
                Auto-evolving multi-agent control plane for KiCad design
                review, repair, and strategy evolution. It closes the loop
                from evaluation to repair to trajectory signal to better
                heuristics.
              </motion.p>
              <motion.a
                href="#console"
                initial={{ y: 20, opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                transition={{ delay: 0.7, duration: 0.8, ease: easeOut }}
                className="group mt-5 inline-flex items-center gap-2 rounded-full bg-primary px-4 py-2 text-sm font-bold text-black transition-all hover:gap-3 sm:text-base"
              >
                Launch a design run
                <span className="flex h-9 w-9 items-center justify-center rounded-full bg-black text-primary transition-transform group-hover:scale-110 sm:h-10 sm:w-10">
                  <ArrowRight size={18} />
                </span>
              </motion.a>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export function SystemSection() {
  return (
    <section id="system" className="bg-black px-4 py-20 sm:px-6 md:py-28">
      <div className="mx-auto max-w-6xl rounded-[1.5rem] bg-[#101010] px-5 py-16 text-center sm:px-8 md:px-12 md:py-24">
        <p className="text-[10px] uppercase tracking-[0.35em] text-primary sm:text-xs">
          AHE control plane
        </p>
        <h2
          className="mx-auto mt-6 max-w-4xl text-3xl leading-[0.98] sm:text-4xl sm:leading-[0.94] md:text-5xl lg:text-6xl xl:text-7xl"
          style={{ color: primaryText }}
        >
          <WordsPullUpMultiStyle
            segments={[
              { text: "This is not a PCB generator." },
              {
                text: "It is a strategy evolution loop.",
                className: "font-serif italic"
              },
              { text: "Every design run becomes training signal." }
            ]}
          />
        </h2>
        <ScrollRevealText text="RatsNest separates governance from intelligence: Spring Boot stores runs and trajectories, Python agents evaluate and repair KiCad projects, ATDP records what happened, and AHE promotes only strategies that pass benchmark gates." />
      </div>
    </section>
  );
}

export function FeaturesSection() {
  const ref = useRef<HTMLDivElement | null>(null);
  const isInView = useInView(ref, { once: true, margin: "-100px 0px" });

  return (
    <section
      id="agents"
      className="relative min-h-screen overflow-hidden bg-black px-4 py-20 sm:px-6 md:py-28"
    >
      <div className="bg-noise absolute inset-0 opacity-[0.15]" />
      <div className="relative mx-auto max-w-7xl">
        <div className="mx-auto max-w-4xl text-center">
          <WordsPullUpMultiStyle
            className="text-xl font-normal leading-tight text-primary sm:text-2xl md:text-3xl lg:text-4xl"
            segments={[
              { text: "Studio-grade autonomy for PCB repair loops." },
              {
                text: "Built for traceability. Powered by evolution.",
                className: "text-gray-500"
              }
            ]}
          />
        </div>

        <div
          ref={ref}
          className="mt-12 grid gap-3 sm:mt-16 md:grid-cols-2 md:gap-2 lg:h-[480px] lg:grid-cols-4 lg:gap-1"
        >
          <motion.div
            initial={{ scale: 0.95, opacity: 0 }}
            animate={isInView ? { scale: 1, opacity: 1 } : { scale: 0.95, opacity: 0 }}
            transition={{ duration: 0.75, ease: [0.22, 1, 0.36, 1] }}
            className="relative min-h-[320px] overflow-hidden rounded-lg bg-[#141414] p-5 lg:h-full"
          >
            <div className="lab-card-bg absolute inset-0" />
            <div className="absolute inset-0 bg-gradient-to-b from-transparent via-black/20 to-black/70" />
            <div className="relative flex h-full flex-col justify-end">
              <p className="text-sm uppercase tracking-[0.3em] text-primary/55">
                live system
              </p>
              <h3 className="mt-3 text-3xl leading-none text-[#E1E0CC] md:text-4xl">
                From run data to better strategy.
              </h3>
            </div>
          </motion.div>

          {features.map((feature, index) => {
            const Icon = feature.icon;
            return (
              <motion.article
                className="flex min-h-[320px] flex-col rounded-lg bg-[#212121] p-5 text-primary lg:h-full"
                initial={{ scale: 0.95, opacity: 0 }}
                animate={
                  isInView ? { scale: 1, opacity: 1 } : { scale: 0.95, opacity: 0 }
                }
                transition={{
                  duration: 0.75,
                  delay: (index + 1) * 0.15,
                  ease: [0.22, 1, 0.36, 1]
                }}
                key={feature.title}
              >
                <div className="flex h-12 w-12 items-center justify-center rounded-md bg-black/45 text-primary">
                  <Icon size={22} />
                </div>
                <div className="mt-8 flex items-start justify-between gap-4">
                  <h3 className="text-xl leading-none text-[#E1E0CC]">
                    {feature.title}
                  </h3>
                  <span className="text-xs text-gray-500">{feature.number}</span>
                </div>
                <p className="mt-4 text-sm leading-relaxed text-gray-400">
                  {feature.copy}
                </p>
                <ul className="mt-6 space-y-3">
                  {feature.checks.map((check) => (
                    <li
                      className="flex items-start gap-2 text-sm text-gray-400"
                      key={check}
                    >
                      <Check className="mt-0.5 text-primary" size={15} />
                      <span>{check}</span>
                    </li>
                  ))}
                </ul>
                <a
                  className="mt-auto inline-flex items-center gap-2 pt-8 text-sm text-primary"
                  href="#console"
                >
                  Learn more
                  <ArrowRight className="-rotate-45" size={16} />
                </a>
              </motion.article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
