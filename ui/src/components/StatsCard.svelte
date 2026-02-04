<script lang="ts">
  export let title: string;
  export let value: number | string;
  export let color: 'cyan' | 'green' | 'pink' | 'yellow' | 'red' = 'cyan';
  export let icon: string = 'bi-activity';

  const colors = {
    cyan: 'border-neon-cyan/30 text-neon-cyan shadow-neon-cyan/20',
    green: 'border-neon-green/30 text-neon-green shadow-neon-green/20',
    pink: 'border-neon-pink/30 text-neon-pink shadow-neon-pink/20',
    yellow: 'border-neon-yellow/30 text-neon-yellow shadow-neon-yellow/20',
    red: 'border-red-500/30 text-red-500 shadow-red-500/20'
  };

  const bgColors = {
    cyan: 'bg-neon-cyan/5 hover:bg-neon-cyan/10',
    green: 'bg-neon-green/5 hover:bg-neon-green/10',
    pink: 'bg-neon-pink/5 hover:bg-neon-pink/10',
    yellow: 'bg-neon-yellow/5 hover:bg-neon-yellow/10',
    red: 'bg-red-500/5 hover:bg-red-500/10'
  };

  $: baseColor = colors[color];
  $: bgColor = bgColors[color];
</script>

<div class="rounded-xl border backdrop-blur-sm p-5 transition-all duration-300 hover:scale-[1.02] hover:shadow-[0_0_15px_rgba(0,0,0,0.3)] {baseColor} {bgColor}">
  <div class="flex justify-between items-start">
    <div>
      <h3 class="text-gray-400 text-xs font-bold uppercase tracking-widest mb-1">{title}</h3>
      <div class="text-4xl font-black tracking-tighter tabular-nums drop-shadow-md">
        {value}
      </div>
    </div>
    <div class="p-2 rounded-lg bg-black/20 text-xl">
      <i class="bi {icon}"></i>
    </div>
  </div>
</div>
