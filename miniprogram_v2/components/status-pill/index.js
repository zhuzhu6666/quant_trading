Component({
  properties: {
    label: {
      type: null,
      value: '--',
      observer(value) {
        const text = value === null || value === undefined || value === '' ? '--' : String(value);
        if (text !== this.data.displayLabel) {
          this.setData({ displayLabel: text });
        }
      },
    },
    tone: {
      type: String,
      value: 'neutral',
      observer(value) {
        const allowed = ['neutral', 'positive', 'negative', 'warning'];
        const nextTone = allowed.includes(String(value || '')) ? String(value) : 'neutral';
        if (nextTone !== this.data.displayTone) {
          this.setData({ displayTone: nextTone });
        }
      },
    },
  },
  data: {
    displayLabel: '--',
    displayTone: 'neutral',
  }
});
